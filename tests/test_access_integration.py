from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from qdrant_client import models
from fastapi import HTTPException

from backend.access import allowed_chunk_ids, allowed_document_ids
from backend.api import download_report, download_report_pdf, get_research, research_sources
from backend.config import Settings
from backend.db import Database
from backend.ingestion import ingest_document
from backend.retrieval import Retriever


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется локальный PostgreSQL")
async def test_domain_role_and_chunk_acl_follow_grant_and_revocation(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
                file_root=tmp_path,
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    email_a, email_b = f"a-{uuid4()}@test.local", f"b-{uuid4()}@test.local"
    async with db.connection() as conn:
        owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id", email_a)
        other = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','reader') RETURNING id", email_b)
        domain = await conn.fetchval("INSERT INTO domains(code,title,classification) VALUES($1,'Тестовый домен','restricted') RETURNING id", f"d-{uuid4().hex}")
        await conn.execute("INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'manager')", domain, owner)
    try:
        document = await ingest_document(
            db, settings, owner, "Секретный пример", b"Restricted industrial pressure setting: 42 bar.",
            "text/plain", "upload", classification="restricted", domain_id=domain,
        )
        async with db.connection() as conn:
            chunk = await conn.fetchval(
                "SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1",
                document,
            )
        assert document in await allowed_document_ids(db, {"id": owner})
        assert document not in await allowed_document_ids(db, {"id": other})
        assert chunk not in await allowed_chunk_ids(db, {"id": other})

        async with db.connection() as conn:
            await conn.execute("INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'reader')", domain, other)
        assert document in await allowed_document_ids(db, {"id": other})
        assert chunk in await allowed_chunk_ids(db, {"id": other})

        async with db.connection() as conn:
            await conn.execute("INSERT INTO chunk_acl(chunk_id,principal_type,principal_id) VALUES($1,'user',$2)", chunk, owner)
        assert chunk not in await allowed_chunk_ids(db, {"id": other})

        async with db.connection() as conn:
            await conn.execute("DELETE FROM chunk_acl WHERE chunk_id=$1", chunk)
            await conn.execute("DELETE FROM domain_members WHERE domain_id=$1 AND user_id=$2", domain, other)
        assert chunk not in await allowed_chunk_ids(db, {"id": other})
    finally:
        async with db.connection() as conn:
            await conn.execute("DELETE FROM documents WHERE id=$1", document)
            await conn.execute("DELETE FROM domains WHERE id=$1", domain)
            await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", [owner, other])
        await db.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется локальный PostgreSQL")
async def test_report_disappears_immediately_after_source_acl_revocation(tmp_path) -> None:
    settings = Settings(
        _env_file=None, file_root=tmp_path,
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    owner = reader = document = research_id = None
    try:
        async with db.connection() as conn:
            owner = await conn.fetchval(
                "INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id",
                f"owner-{uuid4()}@test.local",
            )
            reader = await conn.fetchval(
                "INSERT INTO users(email,password_hash,role) VALUES($1,'test','reader') RETURNING id",
                f"reader-{uuid4()}@test.local",
            )
            domain = await conn.fetchval("INSERT INTO domains(code,title,classification) VALUES($1,'Тестовый домен','restricted') RETURNING id", f"d-{uuid4().hex}")
            await conn.execute("INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'manager'),($1,$3,'reader')", domain, owner, reader)
        document = await ingest_document(
            db, settings, owner, "Ограниченная инструкция", b"Secret pressure is 42 bar.",
            "text/plain", "upload", classification="restricted", domain_id=domain,
        )
        async with db.connection() as conn:
            chunk = await conn.fetchval(
                "SELECT c.id FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1",
                document,
            )
            research_id = await conn.fetchval(
                "INSERT INTO researches(owner_id,title,question,status,visibility,classification) "
                "VALUES($1,'Датчик','Каковы пределы датчика?','completed','public','public') RETURNING id",
                owner,
            )
            await conn.execute(
                "INSERT INTO reports(research_id,version,body) VALUES($1,1,'Секретное давление 42 бар [E1].')",
                research_id,
            )
            await conn.execute(
                "INSERT INTO evidence(research_id,chunk_id,note) VALUES($1,$2,'E1')",
                research_id, chunk,
            )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))
        user = {"id": reader, "role": "reader"}
        before = await get_research(research_id, request, user)
        assert "42 бар" in before["report"]["body"]
        assert [source["marker"] for source in await research_sources(research_id, request, user)] == ["E1"]
        async with db.connection() as conn:
            await conn.execute("DELETE FROM domain_members WHERE domain_id=$1 AND user_id=$2", domain, reader)
        with pytest.raises(HTTPException) as hidden_after_revoke:
            await get_research(research_id, request, user)
        assert hidden_after_revoke.value.status_code == 404
        with pytest.raises(HTTPException) as hidden_sources:
            await research_sources(research_id, request, user)
        assert hidden_sources.value.status_code == 404
        with pytest.raises(HTTPException) as denied:
            await download_report(research_id, request, user)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied_pdf:
            await download_report_pdf(research_id, request, user)
        assert denied_pdf.value.status_code == 404
        async with db.connection() as conn:
            await conn.execute("UPDATE researches SET classification='restricted' WHERE id=$1", research_id)
        with pytest.raises(HTTPException) as hidden:
            await get_research(research_id, request, user)
        assert hidden.value.status_code == 404
    finally:
        async with db.connection() as conn:
            if research_id:
                await conn.execute("DELETE FROM researches WHERE id=$1", research_id)
            if document:
                await conn.execute("DELETE FROM documents WHERE id=$1", document)
            if owner and reader:
                await conn.execute("DELETE FROM domains WHERE id=$1", domain)
            if owner and reader:
                await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", [owner, reader])
        await db.stop()


class FakeEncoder:
    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            return np.array([1.0, 0.0, 0.0])
        return np.array([[1.0, 0.0, 0.0] for _ in texts])


class FakeReranker:
    def predict(self, pairs):
        return [1.0 for _ in pairs]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуются PostgreSQL и Qdrant")
async def test_vector_search_filters_restricted_chunks_before_return(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
                file_root=tmp_path,
        qdrant_collection=f"test_chunks_{uuid4().hex}",
        database_url=os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research"),
    )
    db = Database(settings)
    await db.start()
    email_a, email_b = f"a-{uuid4()}@test.local", f"b-{uuid4()}@test.local"
    async with db.connection() as conn:
        owner = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','researcher') RETURNING id", email_a)
        other = await conn.fetchval("INSERT INTO users(email,password_hash,role) VALUES($1,'test','reader') RETURNING id", email_b)
    retriever = Retriever(db, settings)
    retriever.encoder = FakeEncoder()
    retriever.reranker = FakeReranker()
    if not await retriever.client.collection_exists(retriever.collection):
        await retriever.client.create_collection(retriever.collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    document = None
    try:
        document = await ingest_document(
            db, settings, owner, "Секретная настройка", b"Industrial pressure setting is forty two bar.",
            "text/plain", "upload", classification="restricted",
        )
        await retriever.index_document(document)
        assert await retriever.search("pressure", {"id": other, "role": "reader"}) == []
        found = await retriever.search("pressure", {"id": owner, "role": "researcher"})
        assert len(found) == 1
        assert "forty two" in found[0]["body"]
    finally:
        if document:
            async with db.connection() as conn:
                await conn.execute("DELETE FROM documents WHERE id=$1", document)
                await conn.execute("DELETE FROM users WHERE id=ANY($1::uuid[])", [owner, other])
        await retriever.client.delete_collection(retriever.collection)
        await retriever.close()
        await db.stop()
