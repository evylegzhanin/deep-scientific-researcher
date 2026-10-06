from __future__ import annotations

import os
from uuid import uuid4

import pytest

from backend.config import Settings
from backend.db import Database
from backend.research import ResearchEngine
from backend.vision import describe_document_images


class FakeModel:
    async def generate(self, system: str, user: str) -> str:
        return "УТОЧНЕНИЕ: Какие характеристики датчика нужно сравнить?"

    async def describe_page(self, image_path, question: str) -> str:
        return "На схеме показан датчик давления."


class EchoClarificationModel:
    async def generate(self, system: str, user: str) -> str:
        return "УТОЧНЕНИЕ: Каков предельный порог давления датчика?"


@pytest.mark.asyncio
async def test_planner_asks_once_and_resumes_after_answer() -> None:
    engine = object.__new__(ResearchEngine)
    engine.model = FakeModel()
    events = []

    async def record(research_id, kind, payload):
        events.append((kind, payload))

    engine.event = record
    state = {"research_id": str(uuid4()), "question": "Исследовать датчики"}
    first = await engine.plan(state)
    assert first["clarification"].startswith("Какие характеристики")
    assert engine.route_after_plan(first) == "clarify"
    second = await engine.plan({**state, "question": "Исследовать датчики\nУточнение: точность и диапазон"})
    assert second["clarification"] == ""
    assert engine.route_after_plan(second) == "research"
    assert events[1][0] == "clarification"


@pytest.mark.asyncio
async def test_planner_ignores_echoed_clarification() -> None:
    engine = object.__new__(ResearchEngine)
    engine.model = EchoClarificationModel()

    async def record(*args):
        return None

    engine.event = record
    result = await engine.plan({"research_id": str(uuid4()), "question": "Каков предельный порог давления датчика?"})
    assert result["clarification"] == ""
    assert result["search_queries"] == ["Каков предельный порог давления датчика?"]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется PostgreSQL")
async def test_restricted_diagram_never_goes_to_remote_vlm(tmp_path) -> None:
    common = dict(
        _env_file=None,         database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
        file_root=tmp_path,
    )
    connected = Settings(**common)
    local = Settings(**common, app_mode="airgap", model_base_url="http://localhost:8001/v1", vision_base_url="http://localhost:8002/v1")
    db = Database(local)
    await db.start()
    document_id = public_id = None
    try:
        (tmp_path / "diagram.png").write_bytes(b"synthetic image")
        async with db.connection() as conn:
            document_id = await conn.fetchval("INSERT INTO documents(title,source_kind,classification) VALUES('Схема','upload','restricted') RETURNING id")
            version_id = await conn.fetchval("INSERT INTO document_versions(document_id,sha256,blob_path,media_type) VALUES($1,$2,$3,'application/pdf') RETURNING id", document_id, uuid4().hex * 2, str(tmp_path / "original.pdf"))
            await conn.execute("INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,1,'image','Фрагмент страницы: diagram.png')", version_id)
        model = FakeModel()
        with pytest.raises(ValueError, match="только локальной"):
            await describe_document_images(db, connected, model, document_id, "датчик")
        assert await describe_document_images(db, local, model, document_id, "датчик") == 1
        assert await describe_document_images(db, local, model, document_id, "датчик") == 0
        async with db.connection() as conn:
            public_id = await conn.fetchval(
                "INSERT INTO documents(title,source_kind,classification) VALUES('Открытая схема','upload','public') RETURNING id"
            )
            public_version = await conn.fetchval(
                "INSERT INTO document_versions(document_id,sha256,blob_path,media_type) VALUES($1,$2,$3,'application/pdf') RETURNING id",
                public_id, uuid4().hex * 2, str(tmp_path / "public.pdf"),
            )
            protected_image = await conn.fetchval(
                "INSERT INTO chunks(version_id,page_number,kind,body) VALUES($1,1,'image','Фрагмент страницы: diagram.png') RETURNING id",
                public_version,
            )
            await conn.execute(
                "INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2)",
                protected_image, uuid4(),
            )
        assert await describe_document_images(db, connected, model, public_id, "датчик") == 0
    finally:
        if document_id or public_id:
            async with db.connection() as conn:
                for item in (document_id, public_id):
                    if item:
                        await conn.execute("DELETE FROM documents WHERE id=$1", item)
        await db.stop()
