import json
import os
from uuid import uuid4

import pytest

from backend.access import allowed_chunk_ids
from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from scripts.isolate_benchmark_corpus import isolate


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Requires PostgreSQL")
async def test_quality_corpus_excludes_security_fixture_without_hiding_it_from_owner(tmp_path):
    settings = Settings(_env_file=None, file_root=tmp_path,
                        database_url=os.environ["TEST_DATABASE_URL"])
    db = Database(settings); await db.start()
    owner = core = fixture = quality = None
    email = f"bench-scope-{uuid4()}@test.local"
    quality_email = f"bench-quality-{uuid4()}@test.local"
    try:
        async with db.connection() as conn:
            owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id", email)
        core = await ingest_document(db, settings, owner, "synthetic-core.txt", b"Core pressure 42 bar.", "text/plain", "upload", classification="public")
        fixture = await ingest_document(db, settings, owner, "synthetic-injection.txt", b"Synthetic security injection pressure 42 bar.", "text/plain", "upload", classification="public")
        users = tmp_path / "users.json"
        users.write_text(json.dumps({"a":{"email":email,"password":"unused"}, "quality":{"email":quality_email,"password":"synthetic-password-123"}}))
        corpus = tmp_path / "corpus.json"; corpus.write_text(json.dumps({"documents":[{"id":str(core)}]}))
        output = tmp_path / "scope.json"
        await isolate(users, corpus, output, settings)
        async with db.connection() as conn:
            quality = await conn.fetchval("SELECT id FROM users WHERE email=$1", quality_email)
            fixture_chunk = await conn.fetchval("SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1", fixture)
        assert fixture_chunk not in await allowed_chunk_ids(db, {"id":quality})
        assert fixture_chunk in await allowed_chunk_ids(db, {"id":owner})
        assert json.loads(output.read_text())["passed"]
        assert users.stat().st_mode & 0o777 == 0o600
    finally:
        async with db.connection() as conn:
            await conn.execute("DELETE FROM documents WHERE id=ANY($1::uuid[])", [d for d in (core, fixture) if d])
            await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", [u for u in (owner, quality) if u])
        await db.stop()
