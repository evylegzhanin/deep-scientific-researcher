from __future__ import annotations

import json
import os
from uuid import uuid4

import numpy as np
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from qdrant_client import models

from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from backend.research import ResearchEngine
from backend.jobs import enqueue_run, claim_job
from backend.retrieval import Retriever


class FakeEncoder:
    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            return np.array([1.0, 0.0, 0.0])
        return np.array([[1.0, 0.0, 0.0] for _ in texts])


class FakeReranker:
    def predict(self, pairs):
        return [1.0 for _ in pairs]


class FakeModel:
    def __init__(self):
        self.plan_calls = 0
        self.fail_review_once = True

    async def generate(self, system: str, user: str) -> str:
        if "поисковые запросы" in system or "планировщик" in system:
            self.plan_calls += 1
            return "pressure sensor\nindustrial pressure"
        if "проверяющий" in system:
            if self.fail_review_once:
                self.fail_review_once = False
                raise RuntimeError("temporary model failure")
            blocks = json.loads(user)["blocks"]
            return json.dumps({"status": "ok", "coherent": True, "feedback": "", "blocks": [
                {"id": block["id"], "supported": True, "feedback": ""} for block in blocks
            ]})
        return "В документе указан порог давления 42 бар [E1]."


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуются PostgreSQL и Qdrant")
async def test_airgap_research_builds_cited_report_without_network(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
                app_mode="airgap",
        model_base_url="http://localhost:8001/v1",
        vision_base_url="http://localhost:8002/v1",
        file_root=tmp_path,
        qdrant_collection=f"test_chunks_{uuid4().hex}",
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    email = f"agent-{uuid4()}@test.local"
    async with db.connection() as conn:
        owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id", email)
    retriever = Retriever(db, settings)
    retriever.encoder = FakeEncoder()
    retriever.reranker = FakeReranker()
    if not await retriever.client.collection_exists(retriever.collection):
        await retriever.client.create_collection(retriever.collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    document = research_id = None
    try:
        document = await ingest_document(
            db, settings, owner, "Инструкция датчика давления",
            "Pressure sensor threshold: 42 bar. Проверка датчика обязательна.".encode(),
            "text/plain", "upload", classification="restricted",
        )
        await retriever.index_document(document)
        async with db.connection() as conn:
            research_id = await conn.fetchval(
                "INSERT INTO researches(owner_id,title,question,classification) VALUES($1,'Порог давления','Какой порог pressure sensor?','restricted') RETURNING id",
                owner,
            )
            async with conn.transaction():
                await enqueue_run(conn, research_id, 'Какой порог pressure sensor?')
        claim = await claim_job(db, research_id)
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as saver:
            await saver.setup()
            engine = ResearchEngine(db, settings, retriever, checkpointer=saver)
            fake = FakeModel()
            engine.model = fake
            with pytest.raises(RuntimeError, match="temporary model failure"):
                await engine.run(research_id, claim)
            await engine.run(research_id, claim)
            snapshot = await engine.graph.aget_state({"configurable": {"thread_id": str(claim.run_id)}})
            assert not snapshot.next
            assert fake.plan_calls == 1
        async with db.connection() as conn:
            status = await conn.fetchval("SELECT status FROM researches WHERE id=$1", research_id)
            report = await conn.fetchval("SELECT body FROM reports WHERE research_id=$1", research_id)
            evidence_count = await conn.fetchval("SELECT count(*) FROM evidence WHERE research_id=$1", research_id)
        assert status == "completed"
        assert "42 бар [E1]" in report
        assert "SHA-256" in report
        assert evidence_count == 1
    finally:
        async with db.connection() as conn:
            if research_id:
                await conn.execute("DELETE FROM researches WHERE id=$1", research_id)
            if document:
                await conn.execute("DELETE FROM documents WHERE id=$1", document)
            await conn.execute("DELETE FROM users WHERE id=$1", owner)
        await retriever.client.delete_collection(retriever.collection)
        await retriever.close()
        await db.stop()
