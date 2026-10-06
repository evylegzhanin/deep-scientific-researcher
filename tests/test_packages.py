from __future__ import annotations

import io
import json
import os
import zipfile
from uuid import uuid4

import pytest

from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from backend.packages import export_public_package, import_public_package


def archive_with(manifest: dict, entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../outside", "/absolute", "blobs/../../outside"])
async def test_package_rejects_unsafe_archive_path(name: str) -> None:
    settings = Settings(_env_file=None)
    data = archive_with({"format": "research-source-package-v1", "documents": [{}]}, {name: b"x"})
    with pytest.raises(ValueError):
        await import_public_package(None, settings, None, data)


@pytest.mark.asyncio
async def test_package_rejects_bad_checksum_before_database_access() -> None:
    settings = Settings(_env_file=None)
    digest = "0" * 64
    manifest = {
        "format": "research-source-package-v1",
        "documents": [{"title": "Тест", "source_url": None, "source_kind": "upload", "media_type": "text/plain", "sha256": digest, "size": 3}],
    }
    data = archive_with(manifest, {f"blobs/{digest}": b"bad"})
    with pytest.raises(ValueError, match="Контрольная сумма"):
        await import_public_package(None, settings, None, data)


class NoopRetriever:
    async def index_document(self, document_id):
        return None


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется локальный PostgreSQL")
async def test_public_document_export_import_roundtrip(tmp_path) -> None:
    settings = Settings(
        _env_file=None, file_root=tmp_path,
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    email = f"package-{uuid4()}@test.local"
    async with db.connection() as conn:
        owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','admin') RETURNING id", email)
    imported = None
    try:
        original = await ingest_document(
            db, settings, owner, "Технический материал", b"A public research document.",
            "text/plain", "upload", classification="public",
        )
        package = await export_public_package(db, [original])
        async with db.connection() as conn:
            chunk = await conn.fetchval(
                "SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1",
                original,
            )
            await conn.execute(
                "INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2)",
                chunk, owner,
            )
        with pytest.raises(ValueError, match="ограниченными фрагментами"):
            await export_public_package(db, [original])
        async with db.connection() as conn:
            await conn.execute("DELETE FROM documents WHERE id=$1", original)
        imported = (await import_public_package(db, settings, NoopRetriever(), package))[0]
        async with db.connection() as conn:
            row = await conn.fetchrow("SELECT title,classification FROM documents WHERE id=$1", imported)
        assert row["title"] == "Технический материал"
        assert row["classification"] == "public"
    finally:
        async with db.connection() as conn:
            if imported:
                await conn.execute("DELETE FROM documents WHERE id=$1", imported)
            await conn.execute("DELETE FROM users WHERE id=$1", owner)
        await db.stop()
