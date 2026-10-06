from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status

from backend.db import Database


async def allowed_document_ids(db: Database, user: dict) -> list[UUID]:
    async with db.connection() as conn:
        rows = await conn.fetch(
            """SELECT d.id FROM documents d WHERE
               (d.classification='public' AND d.domain_id=(SELECT id FROM domains WHERE code='public'))
               OR EXISTS(SELECT 1 FROM domain_members m WHERE m.domain_id=d.domain_id AND m.user_id=$1)""",
            user["id"],
        )
    return [row["id"] for row in rows]


async def allowed_chunk_ids(db: Database, user: dict) -> list[UUID]:
    async with db.connection() as conn:
        rows = await conn.fetch(
            """SELECT c.id FROM chunks c
               JOIN document_versions v ON v.id=c.version_id
               JOIN documents d ON d.id=v.document_id
               WHERE ((d.classification='public' AND d.domain_id=(SELECT id FROM domains WHERE code='public'))
                 OR EXISTS(SELECT 1 FROM domain_members m WHERE m.domain_id=d.domain_id AND m.user_id=$1))
               AND (NOT EXISTS(SELECT 1 FROM chunk_acl ca WHERE ca.chunk_id=c.id)
                OR EXISTS(SELECT 1 FROM chunk_acl ca WHERE ca.chunk_id=c.id AND
                  ((ca.principal_type='user' AND ca.principal_id=$1) OR
                   (ca.principal_type='group' AND ca.principal_id IN
                     (SELECT group_id FROM group_members WHERE user_id=$1)))))""",
            user["id"],
        )
    return [row["id"] for row in rows]


async def externally_shareable_chunk_ids(db: Database) -> set[UUID]:
    """Чанки, допустимые к отправке внешней модели независимо от прав владельца."""
    async with db.connection() as conn:
        rows = await conn.fetch(
            """SELECT c.id FROM chunks c
               JOIN document_versions v ON v.id=c.version_id
               JOIN documents d ON d.id=v.document_id
               WHERE d.classification='public' AND d.domain_id=(SELECT id FROM domains WHERE code='public')
                 AND NOT EXISTS(SELECT 1 FROM chunk_acl a WHERE a.chunk_id=c.id)"""
        )
    return {row["id"] for row in rows}


async def require_research(db: Database, research_id: UUID, user: dict) -> dict:
    async with db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM researches WHERE id=$1 AND (owner_id=$2 OR (visibility='public' AND classification='public'))",
            research_id,
            user["id"],
        )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Исследование не найдено")
    if row["owner_id"] != user["id"]:
        async with db.connection() as conn:
            cited = await conn.fetch("SELECT chunk_id FROM evidence WHERE research_id=$1", research_id)
        permitted = set(await allowed_chunk_ids(db, user))
        if any(item["chunk_id"] not in permitted for item in cited):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Исследование не найдено")
    return dict(row)
