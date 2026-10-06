CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin','researcher','reader')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id uuid NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, group_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text UNIQUE NOT NULL,
  csrf_hash text NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE TABLE IF NOT EXISTS domains (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text UNIQUE NOT NULL CHECK (code ~ '^[a-z0-9][a-z0-9_-]{1,63}$'),
  title text NOT NULL,
  classification text NOT NULL CHECK (classification IN ('public','restricted'))
);
INSERT INTO domains(code,title,classification) VALUES('public','Открытые материалы','public')
ON CONFLICT(code) DO NOTHING;
CREATE TABLE IF NOT EXISTS domain_members (
  domain_id uuid NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('reader','researcher','manager')),
  PRIMARY KEY(domain_id,user_id)
);
CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid REFERENCES users(id),
  title text NOT NULL,
  source_url text,
  source_kind text NOT NULL,
  classification text NOT NULL CHECK (classification IN ('public','restricted')),
  domain_id uuid REFERENCES domains(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS domain_id uuid REFERENCES domains(id);
INSERT INTO domains(code,title,classification)
SELECT 'private-' || id::text, 'Личный домен ' || email, 'restricted' FROM users
ON CONFLICT(code) DO NOTHING;
INSERT INTO domain_members(domain_id,user_id,role)
SELECT d.id,u.id,'manager' FROM users u JOIN domains d ON d.code='private-' || u.id::text
ON CONFLICT(domain_id,user_id) DO NOTHING;
UPDATE documents SET domain_id=(SELECT id FROM domains WHERE code='public')
WHERE domain_id IS NULL AND classification='public';
UPDATE documents doc SET domain_id=d.id FROM domains d
WHERE doc.domain_id IS NULL AND doc.classification='restricted'
  AND d.code='private-' || doc.owner_id::text;
CREATE TABLE IF NOT EXISTS document_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  sha256 text NOT NULL,
  blob_path text NOT NULL,
  media_type text NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id, sha256)
);
CREATE TABLE IF NOT EXISTS chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  page_number integer,
  kind text NOT NULL CHECK (kind IN ('text','table','image')),
  body text NOT NULL,
  bbox jsonb,
  search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
);
CREATE INDEX IF NOT EXISTS chunks_search_idx ON chunks USING gin(search_vector);
-- Provenance-backed technical identifier graph: document -> chunk -> entity.
-- Entity visibility is inherited from authorized incident chunks, never global lookup.
CREATE TABLE IF NOT EXISTS knowledge_entities (
  id bigserial PRIMARY KEY,
  identifier text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_entity_mentions (
  chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  entity_id bigint NOT NULL REFERENCES knowledge_entities(id),
  PRIMARY KEY(chunk_id, entity_id)
);
CREATE INDEX IF NOT EXISTS entity_mentions_idx ON chunk_entity_mentions(entity_id,chunk_id);
CREATE TABLE IF NOT EXISTS chunk_acl (
  chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  principal_type text NOT NULL CHECK (principal_type IN ('user','group')),
  principal_id uuid NOT NULL,
  PRIMARY KEY(chunk_id, principal_type, principal_id)
);
CREATE TABLE IF NOT EXISTS researches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid NOT NULL REFERENCES users(id),
  title text NOT NULL,
  question text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public')),
  classification text NOT NULL DEFAULT 'public' CHECK (classification IN ('public','restricted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  error text
);
ALTER TABLE researches ADD COLUMN IF NOT EXISTS classification text NOT NULL DEFAULT 'public'
  CHECK (classification IN ('public','restricted'));
CREATE TABLE IF NOT EXISTS research_events (
  id bigserial PRIMARY KEY,
  research_id uuid NOT NULL REFERENCES researches(id) ON DELETE CASCADE,
  kind text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_events_research_idx ON research_events(research_id, id);
CREATE TABLE IF NOT EXISTS research_jobs (
  research_id uuid PRIMARY KEY REFERENCES researches(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  last_error text
);
CREATE TABLE IF NOT EXISTS messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_id uuid NOT NULL REFERENCES researches(id) ON DELETE CASCADE,
  author text NOT NULL CHECK (author IN ('user','assistant')),
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_id uuid NOT NULL REFERENCES researches(id) ON DELETE CASCADE,
  chunk_id uuid NOT NULL REFERENCES chunks(id),
  note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_id uuid NOT NULL REFERENCES researches(id) ON DELETE CASCADE,
  version integer NOT NULL,
  body text NOT NULL,
  review_status text,
  dropped_claims integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(research_id, version)
);
ALTER TABLE reports ADD COLUMN IF NOT EXISTS review_status text;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS dropped_claims integer;
CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY,
  actor_id uuid,
  action text NOT NULL,
  target text NOT NULL,
  decision text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Each user request has its own immutable question and checkpoint identity.
CREATE TABLE IF NOT EXISTS research_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_id uuid NOT NULL REFERENCES researches(id) ON DELETE CASCADE,
  question text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE research_jobs ADD COLUMN IF NOT EXISTS run_id uuid REFERENCES research_runs(id);
ALTER TABLE research_jobs ADD COLUMN IF NOT EXISTS lease_token uuid;
ALTER TABLE research_events ADD COLUMN IF NOT EXISTS run_id uuid REFERENCES research_runs(id);
ALTER TABLE reports ADD COLUMN IF NOT EXISTS run_id uuid REFERENCES research_runs(id);
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS request_id uuid;
-- Backfill legacy jobs once; old research-level checkpoints are intentionally not reused.
INSERT INTO research_runs(id,research_id,question,status)
SELECT r.id,r.id,r.question,j.status FROM researches r JOIN research_jobs j ON j.research_id=r.id
WHERE j.run_id IS NULL ON CONFLICT(id) DO NOTHING;
UPDATE research_jobs SET run_id=research_id WHERE run_id IS NULL;
ALTER TABLE research_jobs ALTER COLUMN run_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS research_jobs_run_idx ON research_jobs(run_id);
CREATE INDEX IF NOT EXISTS research_runs_research_idx ON research_runs(research_id);
