from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.access import allowed_document_ids
from backend.api import DomainAssignment, DomainCreate, DomainGrant, assign_document_domain, create_domain, grant_domain, revoke_domain
from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется локальный PostgreSQL")
async def test_domain_role_covers_all_documents_and_revocation_is_immediate(tmp_path) -> None:
    settings = Settings(
        _env_file=None, file_root=tmp_path,
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    users = []
    documents = []
    domain_uuid = None
    second_domain_uuid = None
    try:
        async with db.connection() as conn:
            for role in ("admin", "researcher", "reader"):
                users.append(await conn.fetchval(
                    "INSERT INTO users(email,password_hash,role) VALUES($1,'test',$2) RETURNING id",
                    f"{role}-{uuid4()}@test.local", role,
                ))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))
        admin = {"id": users[0], "role": "admin"}
        created = await create_domain(DomainCreate(code=f"d-{uuid4().hex}", title="Испытательный домен"), request, admin)
        domain_uuid = UUID(created["id"])
        await grant_domain(domain_uuid, users[1], DomainGrant(role="researcher"), request, admin)
        for name in ("Первый", "Второй"):
            documents.append(await ingest_document(
                db, settings, users[1], name, f"{name} секретный материал".encode(),
                "text/plain", "upload", classification="restricted", domain_id=domain_uuid,
            ))
        with pytest.raises(ValueError, match="Нет права загрузки"):
            await ingest_document(
                db, settings, users[2], "Запрет", "Недопустимая загрузка".encode(),
                "text/plain", "upload", classification="restricted", domain_id=domain_uuid,
            )
        await grant_domain(domain_uuid, users[2], DomainGrant(role="reader"), request, admin)
        assert set(documents).issubset(set(await allowed_document_ids(db, {"id": users[2]})))
        second = await create_domain(DomainCreate(code=f"d-{uuid4().hex}", title="Другой домен"), request, admin)
        second_domain_uuid = UUID(second["id"])
        await assign_document_domain(documents[1], DomainAssignment(domain_id=second_domain_uuid), request, admin)
        visible = set(await allowed_document_ids(db, {"id": users[2]}))
        assert documents[0] in visible and documents[1] not in visible
        await revoke_domain(domain_uuid, users[2], request, admin)
        assert set(documents).isdisjoint(set(await allowed_document_ids(db, {"id": users[2]})))
    finally:
        async with db.connection() as conn:
            for document in documents:
                await conn.execute("DELETE FROM documents WHERE id=$1", document)
            if domain_uuid:
                await conn.execute("DELETE FROM domains WHERE id=$1", domain_uuid)
            if second_domain_uuid:
                await conn.execute("DELETE FROM domains WHERE id=$1", second_domain_uuid)
            await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", users)
        await db.stop()
