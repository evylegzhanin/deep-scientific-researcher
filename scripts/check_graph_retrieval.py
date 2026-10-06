"""Verify bounded graph traversal and unauthorized bridges using temporary fixtures."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from backend.access import allowed_chunk_ids
from backend.config import Settings
from backend.db import Database
from backend.knowledge_graph import expand, index_chunks


async def check(output):
    db = Database(Settings()); await db.start()
    users, documents, chunks = [], [], []
    domain = None
    # Unique identifiers keep existing documents out of this controlled query.
    suffix = str(int(uuid4().hex[:6], 16))
    keys = [prefix + "-" + suffix for prefix in ("ДД", "ТП", "СК")]
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                for role in ("researcher", "reader"):
                    users.append(await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'unusable-test-hash',$2) RETURNING id", f"graph-{uuid4()}@benchmark.invalid", role))
                domain = await conn.fetchval("INSERT INTO domains(code,title,classification) VALUES($1,'Temporary graph fixture','restricted') RETURNING id", "graph-" + uuid4().hex)
                await conn.execute("INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'manager')", domain, users[0])
                public = await conn.fetchval("SELECT id FROM domains WHERE code='public'")
                bodies = [f"{keys[0]} uses {keys[1]}", f"{keys[1]} requires calibration",
                          f"{keys[0]} uses {keys[2]} SYNTHETIC-SECRET-BRIDGE", f"{keys[2]} downstream"]
                for index, body in enumerate(bodies):
                    restricted = index == 2
                    doc = await conn.fetchval("INSERT INTO documents(owner_id,title,source_kind,classification,domain_id) VALUES($1,$2,'graph-test',$3,$4) RETURNING id",
                                              users[0], f"graph fixture {index}", "restricted" if restricted else "public", domain if restricted else public)
                    documents.append(doc)
                    version = await conn.fetchval("INSERT INTO document_versions(document_id,sha256,blob_path,media_type) VALUES($1,$2,'temporary-graph-fixture','text/plain') RETURNING id", doc, "0" * 64)
                    chunk = await conn.fetchval("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,1,'text',$2) RETURNING id", version, body)
                    chunks.append({"id":chunk, "body":body})
        await index_chunks(db, chunks)
        permitted = await allowed_chunk_ids(db, {"id":users[1], "role":"reader"})
        actual = set(await expand(db, keys[0], permitted))
        expected = {chunks[0]["id"], chunks[1]["id"]}
        checks = {"seed_and_authorized_neighbor_found":actual == expected,
                  "restricted_bridge_excluded":chunks[2]["id"] not in actual,
                  "downstream_of_restricted_bridge_excluded":chunks[3]["id"] not in actual,
                  "no_permission_no_results":await expand(db, keys[0], []) == []}
        result = {"scope":"controlled graph fixtures; does not measure corpus-wide quality benefit", "checks":checks,
                  "passed":all(checks.values()), "query":keys[0], "returned_chunks":len(actual)}
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        if not result["passed"]:
            raise SystemExit(1)
    finally:
        async with db.connection() as conn:
            await conn.execute("DELETE FROM documents WHERE id=ANY($1::uuid[])", documents)
            if domain:
                await conn.execute("DELETE FROM domains WHERE id=$1", domain)
            await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", users)
        await db.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(check(parser.parse_args().output))
