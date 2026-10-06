from __future__ import annotations

import hashlib
import importlib.util
import io
import re
import subprocess
import tempfile
import asyncio
from pathlib import Path
from uuid import UUID

from pypdf import PdfReader
from trafilatura import extract

from backend.config import Settings
from backend.db import Database


def split_text(text: str, max_chars: int = 1800, overlap: int = 180) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", text).strip()
    if not clean:
        return []
    pieces = []
    start = 0
    while start < len(clean):
        end = min(start + max_chars, len(clean))
        if end < len(clean):
            boundary = clean.rfind("\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        part = clean[start:end].strip()
        if part:
            pieces.append(part)
        if end == len(clean):
            break
        start = max(end - overlap, start + 1)
    return pieces


def read_pdf_pages(data: bytes, root: Path, sha: str, perform_ocr: bool = True) -> list[tuple[int, str, Path | None]]:
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > 200:
        raise ValueError("PDF содержит более 200 страниц")
    pages: list[tuple[int, str, Path | None]] = []
    with tempfile.TemporaryDirectory() as temp:
        pdf_path = Path(temp) / "source.pdf"
        pdf_path.write_bytes(data)
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            image_path = root / f"{sha}-page-{number}.png"
            if len(text.strip()) < 100 or len(page.images) > 0:
                subprocess.run(
                    ["pdftoppm", "-f", str(number), "-l", str(number), "-scale-to", "1500", "-singlefile", "-png", str(pdf_path), str(image_path.with_suffix(""))],
                    check=True, timeout=60, capture_output=True,
                )
                if perform_ocr and len(text.strip()) < 100:
                    ocr = subprocess.run(["tesseract", str(image_path), "stdout", "-l", "rus+eng"], check=True, timeout=60, capture_output=True)
                    text = ocr.stdout.decode("utf-8", errors="replace")
            else:
                image_path = None
            pages.append((number, text, image_path))
    return pages


def read_docling_items(data: bytes, settings: Settings) -> list[tuple[int, str, str]]:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    languages = subprocess.run(
        ["tesseract", "--list-langs"], check=True, capture_output=True, text=True, timeout=10,
    ).stdout.splitlines()
    available = set(languages)
    if settings.app_mode == "airgap" and "rus" not in available:
        raise RuntimeError("В закрытом контуре отсутствует русская OCR-модель Tesseract")
    ocr_languages = [language for language in ("rus", "eng") if language in available]
    if not ocr_languages:
        raise RuntimeError("Отсутствуют OCR-модели Tesseract")
    options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        enable_remote_services=False,
        allow_external_plugins=False,
        artifacts_path=settings.docling_artifacts_path,
        ocr_options=TesseractCliOcrOptions(lang=ocr_languages),
    )
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "document.pdf"
        path.write_bytes(data)
        document = converter.convert(path).document
    extracted = []
    seen: set[tuple[int, str, str]] = set()
    for item, _ in document.iterate_items():
        provenance = getattr(item, "prov", []) or []
        page = provenance[0].page_no if provenance else 1
        label = str(getattr(item, "label", "")).lower()
        if "table" in label and hasattr(item, "export_to_markdown"):
            body = item.export_to_markdown(document)
            kind = "table"
        else:
            body = getattr(item, "text", "")
            kind = "text"
        if isinstance(body, str) and body.strip():
            key = (page, kind, body.strip())
            if key not in seen:
                extracted.append(key)
                seen.add(key)
    return extracted


def should_use_docling(settings: Settings) -> bool:
    if settings.pdf_parser == "docling":
        if importlib.util.find_spec("docling") is None:
            raise RuntimeError("Профиль Docling выбран, но пакет не установлен")
        return True
    return (
        settings.pdf_parser == "auto"
        and importlib.util.find_spec("docling") is not None
        and (settings.app_mode != "airgap" or settings.docling_artifacts_path is not None)
    )


async def ingest_document(
    db: Database,
    settings: Settings,
    owner_id: UUID | None,
    title: str,
    data: bytes,
    media_type: str,
    source_kind: str,
    source_url: str | None = None,
    classification: str = "public",
    domain_id: UUID | None = None,
) -> UUID:
    if not data or len(data) > settings.max_document_bytes:
        raise ValueError("Недопустимый размер документа")
    if media_type not in {"application/pdf", "text/html", "text/plain"}:
        raise ValueError("Поддерживаются PDF, HTML и обычный текст")
    if classification not in {"public", "restricted"}:
        raise ValueError("Неверная классификация")
    if domain_id is not None:
        if classification != "restricted" or owner_id is None:
            raise ValueError("Общий домен разрешён только для закрытой загрузки пользователя")
        async with db.connection() as conn:
            permitted = await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM domains d JOIN domain_members m ON m.domain_id=d.id
                   WHERE d.id=$1 AND d.classification='restricted' AND m.user_id=$2
                   AND m.role IN ('researcher','manager'))""", domain_id, owner_id,
            )
        if not permitted:
            raise ValueError("Нет права загрузки в закрытый домен")
    digest = hashlib.sha256(data).hexdigest()
    settings.file_root.mkdir(parents=True, exist_ok=True)
    path = settings.file_root / digest
    if not path.exists():
        path.write_bytes(data)
    if media_type == "application/pdf":
        use_docling = should_use_docling(settings)
        pages = await asyncio.to_thread(read_pdf_pages, data, settings.file_root, digest, not use_docling)
        docling_items = await asyncio.to_thread(read_docling_items, data, settings) if use_docling else []
    elif media_type == "text/html":
        text = extract(data.decode("utf-8", errors="replace"), include_tables=True) or ""
        pages = [(1, text, None)]
        docling_items = []
    else:
        pages = [(1, data.decode("utf-8", errors="replace"), None)]
        docling_items = []
    docling_pages: dict[int, list[str]] = {}
    for number, kind, body in docling_items:
        if kind == "text":
            docling_pages.setdefault(number, []).append(body)
    async with db.connection() as conn:
        async with conn.transaction():
            if classification == "public":
                if domain_id is not None:
                    raise ValueError("Открытый документ относится к общему домену")
                domain_id = await conn.fetchval("SELECT id FROM domains WHERE code='public'")
            elif domain_id is None:
                if owner_id is None:
                    raise ValueError("Закрытому документу нужен владелец и домен")
                domain_id = await conn.fetchval(
                    """INSERT INTO domains(code,title,classification)
                       SELECT 'private-' || id::text,'Личный домен ' || email,'restricted'
                       FROM users WHERE id=$1 ON CONFLICT(code) DO UPDATE SET title=EXCLUDED.title
                       RETURNING id""", owner_id,
                )
                await conn.execute(
                    """INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'manager')
                       ON CONFLICT(domain_id,user_id) DO NOTHING""", domain_id, owner_id,
                )
            else:
                allowed = await conn.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM domains d JOIN domain_members m ON m.domain_id=d.id
                       WHERE d.id=$1 AND d.classification='restricted' AND m.user_id=$2
                       AND m.role IN ('researcher','manager'))""", domain_id, owner_id,
                )
                if not allowed:
                    raise ValueError("Нет права загрузки в закрытый домен")
            existing = await conn.fetchval(
                """SELECT d.id FROM document_versions v JOIN documents d ON d.id=v.document_id
                   WHERE v.sha256=$1 AND d.source_url IS NOT DISTINCT FROM $2
                     AND d.classification=$3 AND (d.owner_id IS NOT DISTINCT FROM $4)
                     AND d.domain_id=$5""",
                digest, source_url, classification, owner_id, domain_id,
            )
            if existing:
                return existing
            document_id = await conn.fetchval(
                "INSERT INTO documents(owner_id,title,source_url,source_kind,classification,domain_id) VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
                owner_id, title[:500], source_url, source_kind, classification, domain_id,
            )
            version_id = await conn.fetchval(
                "INSERT INTO document_versions(document_id,sha256,blob_path,media_type) VALUES($1,$2,$3,$4) RETURNING id",
                document_id, digest, str(path), media_type,
            )
            for number, text, image_path in pages:
                structured_text = "\n".join(docling_pages.get(number, [])) or text
                for part in split_text(structured_text):
                    await conn.execute("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,$2,'text',$3)", version_id, number, part)
                if image_path:
                    await conn.execute("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,$2,'image',$3)", version_id, number, f"Фрагмент страницы: {image_path.name}")
            for number, kind, body in docling_items:
                if kind == "table":
                    for part in split_text(body):
                        await conn.execute("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,$2,'table',$3)", version_id, number, part)
            await conn.execute("INSERT INTO audit_log(actor_id,action,target,decision) VALUES($1,'ingest',$2,'allow')", owner_id, str(document_id))
    return document_id
