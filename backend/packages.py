from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from backend.retrieval import Retriever
from backend.sources import validate_url

MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_DOCUMENTS = 10


async def export_public_package(db: Database, document_ids: list[UUID]) -> bytes:
    if not document_ids or len(document_ids) > MAX_PACKAGE_DOCUMENTS:
        raise ValueError("Пакет должен содержать от 1 до 10 документов")
    async with db.connection() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (d.id) d.id,d.title,d.source_url,d.source_kind,
                      v.id AS version_id,v.sha256,v.blob_path,v.media_type,v.fetched_at
               FROM documents d JOIN document_versions v ON v.document_id=d.id
               WHERE d.id=ANY($1::uuid[]) AND d.classification='public'
               ORDER BY d.id,v.fetched_at DESC""",
            document_ids,
        )
    if len(rows) != len(set(document_ids)):
        raise ValueError("Некоторые документы отсутствуют или не являются публичными")
    async with db.connection() as conn:
        protected = await conn.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM chunks c JOIN chunk_acl a ON a.chunk_id=c.id
                 WHERE c.version_id=ANY($1::uuid[]))""",
            [row["version_id"] for row in rows],
        )
    if protected:
        raise ValueError("Пакет не может содержать документ с ограниченными фрагментами")
    manifest = {"format": "research-source-package-v1", "documents": []}
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            data = Path(row["blob_path"]).read_bytes()
            total += len(data)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Пакет превышает 100 МБ")
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Контрольная сумма сохранённого документа не совпала")
            archive.writestr(f"blobs/{row['sha256']}", data)
            manifest["documents"].append({
                "title": row["title"], "source_url": row["source_url"],
                "source_kind": row["source_kind"], "media_type": row["media_type"],
                "sha256": row["sha256"], "size": len(data),
                "fetched_at": row["fetched_at"].isoformat(),
            })
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue()


async def import_public_package(
    db: Database, settings: Settings, retriever: Retriever, package_data: bytes,
) -> list[UUID]:
    if len(package_data) > MAX_PACKAGE_BYTES:
        raise ValueError("Пакет превышает 100 МБ")
    with zipfile.ZipFile(io.BytesIO(package_data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("Повторные или отсутствующие элементы архива")
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or item.is_dir() or item.flag_bits & 1:
                raise ValueError("Недопустимый путь или зашифрованный файл")
            if item.file_size > settings.max_document_bytes:
                raise ValueError("Документ превышает предел размера")
        manifest = json.loads(archive.read("manifest.json"))
        documents = manifest.get("documents", [])
        if manifest.get("format") != "research-source-package-v1" or not isinstance(documents, list):
            raise ValueError("Неизвестный формат пакета")
        if not 1 <= len(documents) <= MAX_PACKAGE_DOCUMENTS:
            raise ValueError("Недопустимое число документов")
        total_uncompressed = 0
        for item in documents:
            if not isinstance(item, dict):
                raise ValueError("Недопустимая запись документа")
            if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("Недопустимая контрольная сумма")
            if not isinstance(item.get("size"), int) or not 0 < item["size"] <= settings.max_document_bytes:
                raise ValueError("Недопустимый размер документа")
            if not isinstance(item.get("title"), str) or not 0 < len(item["title"]) <= 500:
                raise ValueError("Недопустимое название")
            if item.get("source_kind") not in {"arxiv", "habr", "documentation", "upload"}:
                raise ValueError("Недопустимый тип источника")
            if item.get("source_url") is not None:
                validate_url(item["source_url"])
            total_uncompressed += item["size"]
        if total_uncompressed > MAX_PACKAGE_BYTES:
            raise ValueError("Распакованный пакет превышает 100 МБ")
        expected = {"manifest.json"} | {f"blobs/{item['sha256']}" for item in documents}
        if set(names) != expected:
            raise ValueError("Архив содержит лишние или отсутствующие файлы")
        verified = []
        for item in documents:
            if item.get("media_type") not in {"application/pdf", "text/html", "text/plain"}:
                raise ValueError("Недопустимый тип документа")
            data = archive.read(f"blobs/{item['sha256']}")
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("Контрольная сумма пакета не совпала")
            verified.append((item, data))
    results = []
    for item, data in verified:
        document_id = await ingest_document(
            db, settings, None, item["title"], data, item["media_type"],
            item["source_kind"], item.get("source_url"), classification="public",
        )
        await retriever.index_document(document_id)
        results.append(document_id)
    return results
