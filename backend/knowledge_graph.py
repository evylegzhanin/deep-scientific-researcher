"""Small deterministic identifier graph with chunk-level provenance and ACL filtering."""
from __future__ import annotations

import re
import unicodedata
from uuid import UUID

IDENTIFIER = re.compile(r"(?<!\w)[A-ZА-ЯЁ]{2,8}[-–—]\d{1,8}(?!\w)", re.IGNORECASE)


def identifiers(text: str) -> list[str]:
    return sorted({unicodedata.normalize("NFKC", s).upper().replace("–", "-").replace("—", "-")
                   for s in IDENTIFIER.findall(text)})


async def index_chunks(db, chunks: list[dict]) -> None:
    async with db.connection() as conn:
        async with conn.transaction():
            for chunk in chunks:
                await conn.execute("DELETE FROM chunk_entity_mentions WHERE chunk_id=$1", chunk["id"])
                for identifier in identifiers(chunk["body"]):
                    entity = await conn.fetchval(
                        "INSERT INTO knowledge_entities(identifier) VALUES($1) "
                        "ON CONFLICT(identifier) DO UPDATE SET identifier=EXCLUDED.identifier RETURNING id",
                        identifier,
                    )
                    await conn.execute(
                        "INSERT INTO chunk_entity_mentions(chunk_id,entity_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                        chunk["id"], entity,
                    )


async def expand(db, query: str, permitted: list[UUID], *, max_chunks: int = 50) -> list[UUID]:
    keys = identifiers(query)
    if not keys or not permitted:
        return []
    async with db.connection() as conn:
        rows = await conn.fetch(
            """WITH seeds AS (
                 SELECT DISTINCT m.chunk_id FROM chunk_entity_mentions m
                 JOIN knowledge_entities e ON e.id=m.entity_id
                 WHERE e.identifier=ANY($1::text[]) AND m.chunk_id=ANY($2::uuid[])
               ), neighbors AS (
                 SELECT DISTINCT entity_id FROM chunk_entity_mentions
                 WHERE chunk_id IN (SELECT chunk_id FROM seeds)
               )
               SELECT m.chunk_id, CASE WHEN m.chunk_id IN (SELECT chunk_id FROM seeds)
                 THEN 0 ELSE 1 END AS distance
               FROM chunk_entity_mentions m
               WHERE m.entity_id IN (SELECT entity_id FROM neighbors) AND m.chunk_id=ANY($2::uuid[])
               GROUP BY m.chunk_id ORDER BY distance,m.chunk_id LIMIT $3""",
            keys, permitted, max_chunks,
        )
    return [r["chunk_id"] for r in rows]
