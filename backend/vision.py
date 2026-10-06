from __future__ import annotations

from uuid import UUID

from backend.config import Settings, is_local_model_endpoint
from backend.db import Database
from backend.models import ModelGateway


async def describe_document_images(
    db: Database, settings: Settings, model: ModelGateway, document_id: UUID,
    question: str, limit: int = 5,
) -> int:
    """Описание страниц только через разрешённый модельный endpoint."""
    async with db.connection() as conn:
        document = await conn.fetchrow("SELECT classification FROM documents WHERE id=$1", document_id)
        if not document:
            raise ValueError("Документ не найден")
        vision_url = settings.vision_base_url or settings.model_base_url
        if document["classification"] != "public" and not is_local_model_endpoint(vision_url):
            raise ValueError("Закрытые изображения можно анализировать только локальной моделью")
        local = is_local_model_endpoint(vision_url)
        # OCR/PII redaction of text does not sanitize the original pixels.
        # External originals require an explicit operator opt-in, even when public.
        if not local and not settings.allow_external_images:
            return 0
        images = await conn.fetch(
            """SELECT c.id,c.body FROM chunks c
               JOIN document_versions v ON v.id=c.version_id
               WHERE v.document_id=$1 AND c.kind='image'
                 AND c.body LIKE 'Фрагмент страницы: %'
                 AND ($3::bool OR NOT EXISTS(
                   SELECT 1 FROM chunks sibling JOIN chunk_acl a ON a.chunk_id=sibling.id
                   WHERE sibling.version_id=c.version_id))
               ORDER BY c.page_number LIMIT $2""",
            document_id, limit, is_local_model_endpoint(vision_url),
        )
    count = 0
    for row in images:
        filename = row["body"].removeprefix("Фрагмент страницы: ")
        image_path = settings.file_root / filename
        if image_path.parent.resolve() != settings.file_root.resolve() or not image_path.is_file():
            continue
        description = await model.describe_page(image_path, question)
        async with db.connection() as conn:
            await conn.execute("UPDATE chunks SET body=$2 WHERE id=$1", row["id"], description[:4000])
        count += 1
    return count
