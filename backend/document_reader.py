"""Authorized document preview; original HTML is never executed in the application."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Request
from pypdf import PdfReader

from backend.access import allowed_chunk_ids, allowed_document_ids


async def readable_document(document_id: UUID, request: Request, user: dict) -> tuple[dict, list[dict], Path]:
    db = request.app.state.db
    if document_id not in await allowed_document_ids(db, user):
        raise HTTPException(404, "Документ не найден")
    async with db.connection() as conn:
        row = await conn.fetchrow(
            """SELECT d.title,d.classification,d.source_url,v.blob_path,v.media_type,
                      v.sha256,v.id AS version_id
               FROM documents d JOIN document_versions v ON v.document_id=d.id
               WHERE d.id=$1 ORDER BY v.fetched_at DESC,v.id DESC LIMIT 1""", document_id,
        )
        chunks = await conn.fetch(
            "SELECT id,page_number,kind,body FROM chunks WHERE version_id=$1 ORDER BY page_number,id",
            row["version_id"],
        ) if row else []
    if not row:
        raise HTTPException(404, "Документ не найден")
    allowed = set(await allowed_chunk_ids(db, user))
    if any(chunk["id"] not in allowed for chunk in chunks):
        raise HTTPException(403, "Часть документа закрыта")
    path = Path(row["blob_path"])
    if not path.is_file() or path.resolve().parent != request.app.state.settings.file_root.resolve():
        raise HTTPException(404, "Файл не найден")
    return dict(row), [dict(chunk) for chunk in chunks], path


def preview_pages(path: Path, media_type: str, chunks: list[dict]) -> list[dict]:
    if media_type == "text/plain":
        return [{"number": 1, "text": path.read_text(encoding="utf-8", errors="replace")}]
    if media_type == "application/pdf":
        pages = []
        for number, page in enumerate(PdfReader(path).pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                text = "\n\n".join(c["body"] for c in chunks if c["page_number"] == number and c["kind"] == "text")
            pages.append({"number": number, "text": text})
        return pages
    # HTML is shown only as ingested text, never as an active embedded document.
    return [{"number": 1, "text": "\n\n".join(c["body"] for c in chunks if c["kind"] in {"text", "table"})}]
