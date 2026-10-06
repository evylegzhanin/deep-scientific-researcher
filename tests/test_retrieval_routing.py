from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest

from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from backend.retrieval import Retriever


@pytest.mark.asyncio
async def test_openrouter_embedding_request_contains_only_supplied_public_text(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,         model_api_key="synthetic-key", model_base_url="https://openrouter.ai/api/v1",
    )
    retriever = Retriever(None, settings)
    other_model = Settings(
        _env_file=None,         model_base_url="https://openrouter.ai/api/v1", openrouter_embedding_model="other/model",
    )
    other_retriever = Retriever(None, other_model)
    assert retriever.collection != other_retriever.collection
    await other_retriever.close()
    original_client = httpx.AsyncClient
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond)))
    try:
        assert await retriever._remote_vectors(["Открытый синтетический текст"]) == [[1.0, 0.0, 0.0]]
        assert seen[0].url == "https://openrouter.ai/api/v1/embeddings"
        assert seen[0].headers["Authorization"] == "Bearer synthetic-key"
        assert "Открытый синтетический текст" in seen[0].content.decode()
    finally:
        await retriever.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуются PostgreSQL и Qdrant")
async def test_connected_mode_never_sends_restricted_text_or_question_to_embeddings(tmp_path) -> None:
    settings = Settings(
        _env_file=None,         database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
        model_base_url="https://openrouter.ai/api/v1", model_api_key="synthetic-key",
        file_root=tmp_path, qdrant_collection=f"routing_{uuid4().hex}",
    )
    db = Database(settings)
    await db.start()
    retriever = Retriever(db, settings)
    owner = public = restricted = pii_document = protected_document = None
    calls = []

    async def fake_remote(texts):
        calls.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    retriever._remote_vectors = fake_remote
    try:
        async with db.connection() as conn:
            owner = await conn.fetchval(
                "INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id",
                f"routing-{uuid4()}@test.local",
            )
        public = await ingest_document(
            db, settings, owner, "Открытая инструкция", b"Public pressure is 42 bar.",
            "text/plain", "upload", classification="public",
        )
        restricted = await ingest_document(
            db, settings, owner, "Закрытая инструкция", b"Secret pressure is 99 bar.",
            "text/plain", "upload", classification="restricted",
        )
        pii_document = await ingest_document(
            db, settings, owner, "Открытая запись с PII", b"Contact private@example.org for pressure data.",
            "text/plain", "upload", classification="public",
        )
        protected_document = await ingest_document(
            db, settings, owner, "Открытый документ с закрытым чанком", b"Protected pressure is 77 bar.",
            "text/plain", "upload", classification="public",
        )
        async with db.connection() as conn:
            protected_chunk = await conn.fetchval(
                "SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1",
                protected_document,
            )
            await conn.execute(
                "INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2)",
                protected_chunk, owner,
            )
        await retriever.index_document(public)
        await retriever.index_document(restricted)
        await retriever.index_document(pii_document)
        await retriever.index_document(protected_document)
        assert any("Public pressure" in text for text in calls)
        assert not any("Secret pressure" in text for text in calls)
        assert not any("private@example.org" in text for text in calls)
        assert not any("Protected pressure" in text for text in calls)
        calls.clear()
        private_results = await retriever.search("Secret pressure", {"id": owner, "role": "researcher"})
        assert any(item["document_id"] == restricted for item in private_results)
        assert calls == []
        public_results = await retriever.search("Public pressure", {"id": owner, "role": "researcher"}, public_only=True)
        assert any(item["document_id"] == public for item in public_results)
        assert calls == ["Public pressure"]
        protected_results = await retriever.search("Protected pressure", {"id": owner, "role": "researcher"}, public_only=True)
        assert all(item["document_id"] != protected_document for item in protected_results)
        local = Settings(
            _env_file=None, app_mode="airgap",
            model_base_url="http://localhost:8001/v1", qdrant_collection=settings.qdrant_collection,
        )
        local_retriever = Retriever(db, local)
        assert local_retriever.collection != retriever.collection
        await local_retriever.close()
    finally:
        async with db.connection() as conn:
            for document in (public, restricted, pii_document, protected_document):
                if document:
                    await conn.execute("DELETE FROM documents WHERE id=$1", document)
            if owner:
                await conn.execute("DELETE FROM users WHERE id=$1", owner)
        if await retriever.client.collection_exists(retriever.collection):
            await retriever.client.delete_collection(retriever.collection)
        await retriever.close()
        await db.stop()
