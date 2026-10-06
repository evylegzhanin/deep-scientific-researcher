"""Separate a fixed quality/load corpus from synthetic security-test fixtures."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
from uuid import UUID

from backend.access import allowed_chunk_ids
from backend.auth import password_hasher
from backend.config import Settings
from backend.db import Database


async def isolate(users_path, corpus_path, output_path, settings=None):
    accounts = json.loads(users_path.read_text())
    prepared = json.loads(corpus_path.read_text())
    core = [UUID(doc["id"]) for doc in prepared["documents"]]
    account = accounts.get("quality") or {"email":f"benchmark-quality-{secrets.token_hex(6)}@example.local", "password":secrets.token_urlsafe(24)}
    # Persist the generated secret before creating users/ACLs so a failed
    # provisioning attempt can be retried with the same credentials.
    accounts["quality"] = account
    users_path.chmod(0o600)
    users_path.write_text(json.dumps(accounts, indent=2) + "\n")
    db = Database(settings or Settings()); await db.start()
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                owner = await conn.fetchval("SELECT id FROM users WHERE email=$1", accounts["a"]["email"])
                if owner is None:
                    raise ValueError("Benchmark account A not found")
                quality = await conn.fetchval("SELECT id FROM users WHERE email=$1", account["email"])
                if quality is None:
                    quality = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,$2,'researcher') RETURNING id", account["email"], password_hasher.hash(account["password"]))
                scoped = await conn.fetch("SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=ANY($1::uuid[])", core)
                fixtures = await conn.fetch("""SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=v.document_id WHERE d.owner_id=$1 AND d.classification='public'
                    AND d.title LIKE 'synthetic-%' AND NOT d.id=ANY($2::uuid[])""", owner, core)
                for row in fixtures:
                    await conn.execute("INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2) ON CONFLICT DO NOTHING", row["id"], owner)
        actual = set(await allowed_chunk_ids(db, {"id":quality, "role":"researcher"}))
        expected = {row["id"] for row in scoped}
        result = {"scope":"quality/load account sees only prepared corpus chunks; security A retains its fixtures",
                  "core_document_ids":[str(doc) for doc in core], "core_documents":len(core),
                  "core_chunks":len(expected), "excluded_fixture_chunks":len(fixtures),
                  "permitted_chunks":len(actual), "passed":actual == expected and bool(expected)}
        output_path.write_text(json.dumps(result, indent=2) + "\n")
        if not result["passed"]:
            raise ValueError("Quality account's scope differs from prepared corpus; do not benchmark")
        print(json.dumps(result), flush=True)
    finally:
        await db.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(isolate(args.users, args.corpus, args.output))
