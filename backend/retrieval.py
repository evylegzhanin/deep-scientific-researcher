from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
from qdrant_client import AsyncQdrantClient, models
from sentence_transformers import SentenceTransformer

from backend.access import allowed_chunk_ids, externally_shareable_chunk_ids
from backend.config import Settings, is_local_model_endpoint
from backend.db import Database
from backend.guardrails import contains_pii
from backend.reranking import load_reranker
from backend import knowledge_graph

class Retriever:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.client = AsyncQdrantClient(url=settings.qdrant_url, timeout=30)
        endpoint = urlparse(settings.model_base_url)
        self.remote = (
            settings.app_mode == "connected" and endpoint.scheme == "https"
            and endpoint.hostname == "openrouter.ai" and not is_local_model_endpoint(settings.model_base_url)
        )
        provider = "openrouter" if self.remote else "local"
        model_name = settings.openrouter_embedding_model if self.remote else settings.embedding_model
        model_id = hashlib.sha256(f"{provider}:{model_name}".encode()).hexdigest()[:12]
        self.collection = f"{settings.qdrant_collection}_{provider}_{model_id}"
        self.encoder: SentenceTransformer | None = None
        self.reranker = None
        self._encoder_lock = asyncio.Lock()
        self._reranker_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.close()

    async def _model(self) -> SentenceTransformer:
        if self.settings.app_mode != "airgap":
            raise RuntimeError("Локальные embeddings доступны только в закрытом профиле")
        async with self._encoder_lock:
            if self.encoder is None:
                if not Path(self.settings.embedding_model).is_dir():
                    raise RuntimeError("Локальные веса embeddings не найдены")
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                self.encoder = await asyncio.to_thread(SentenceTransformer, self.settings.embedding_model)
            return self.encoder

    async def _remote_vectors(self, texts: list[str]) -> list[list[float]]:
        if not self.remote or not self.settings.model_api_key:
            raise RuntimeError("Для публичных embeddings нужен ключ OpenRouter")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.post(
                f"{self.settings.model_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                json={"model": self.settings.openrouter_embedding_model, "input": texts},
            )
            response.raise_for_status()
        entries = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in entries]
        if len(vectors) != len(texts) or not vectors or len({len(vector) for vector in vectors}) != 1:
            raise RuntimeError("OpenRouter вернул несовместимые embeddings")
        return vectors

    async def _ensure_collection(self, size: int) -> None:
        if not await self.client.collection_exists(self.collection):
            await self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
            )

    async def _reranker(self):
        if self.settings.app_mode != "airgap":
            raise RuntimeError("Локальный reranker доступен только в закрытом профиле")
        async with self._reranker_lock:
            if self.reranker is None:
                if not Path(self.settings.rerank_model).is_dir():
                    raise RuntimeError("Локальные веса reranker не найдены")
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                self.reranker = await asyncio.to_thread(load_reranker, self.settings.rerank_model)
            return self.reranker

    async def index_document(self, document_id: UUID) -> None:
        async with self.db.connection() as conn:
            rows = await conn.fetch(
                """SELECT c.id,c.body,c.kind,c.page_number,d.classification,
                          EXISTS(SELECT 1 FROM chunk_acl a WHERE a.chunk_id=c.id) AS protected
                   FROM chunks c
                   JOIN document_versions v ON v.id=c.version_id
                   JOIN documents d ON d.id=v.document_id
                   WHERE v.document_id=$1 AND c.kind IN ('text','table','image')""",
                document_id,
            )
        if self.settings.app_mode == "connected":
            if not self.remote:
                return
            rows = [
                row for row in rows
                if row["classification"] == "public" and not row["protected"]
                and not contains_pii(row["body"])
            ]
        if not rows:
            return
        await knowledge_graph.index_chunks(self.db, [dict(row) for row in rows])
        if self.remote:
            vectors = await self._remote_vectors([row["body"][:1800] for row in rows])
        else:
            model = await self._model()
            vectors = await asyncio.to_thread(model.encode, [row["body"] for row in rows], normalize_embeddings=True)
        await self._ensure_collection(len(vectors[0]))
        points = [
            models.PointStruct(
                id=str(row["id"]), vector=vector if self.remote else vector.tolist(),
                payload={"document_id": str(document_id), "kind": row["kind"], "page": row["page_number"]},
            )
            for row, vector in zip(rows, vectors)
        ]
        await self.client.upsert(collection_name=self.collection, points=points, wait=True)

    async def search(self, query: str, user: dict, limit: int = 12, public_only: bool = False) -> list[dict]:
        permitted_ids = await allowed_chunk_ids(self.db, user)
        if public_only and permitted_ids:
            shareable = await externally_shareable_chunk_ids(self.db)
            permitted_ids = [item for item in permitted_ids if item in shareable]
        if not permitted_ids:
            return []
        async with self.db.connection() as conn:
            lexical = await conn.fetch(
                """SELECT c.id,ts_rank_cd(c.search_vector, plainto_tsquery('simple',$1)) AS rank
                   FROM chunks c WHERE c.id=ANY($2::uuid[])
                    AND c.search_vector @@ plainto_tsquery('simple',$1)
                   ORDER BY rank DESC LIMIT 50""",
                query, permitted_ids,
            )
        vector_points = []
        use_vectors = self.settings.app_mode == "airgap" or (self.remote and public_only and not contains_pii(query))
        if use_vectors and await self.client.collection_exists(self.collection):
            if self.remote:
                vector = (await self._remote_vectors([query]))[0]
            else:
                model = await self._model()
                vector = (await asyncio.to_thread(model.encode, query, normalize_embeddings=True)).tolist()
            # HasId-предикат применяется до векторного поиска: запрещённые чанки не участвуют в выдаче.
            vector_result = await self.client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=models.Filter(must=[models.HasIdCondition(has_id=[str(value) for value in permitted_ids])]),
                limit=50,
                with_payload=False,
            )
            vector_points = vector_result.points
        scores: dict[UUID, float] = {}
        for rank, row in enumerate(lexical, start=1):
            scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank)
        for rank, point in enumerate(vector_points, start=1):
            chunk_id = UUID(str(point.id))
            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
        if self.settings.graph_retrieval_enabled:
            graph_ids = await knowledge_graph.expand(self.db, query, permitted_ids)
            for rank, chunk_id in enumerate(graph_ids, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
        ordered = sorted(scores, key=scores.get, reverse=True)[: min(limit * 3, 30)]
        if not ordered:
            return []
        # Повторная проверка защищает от изменения ACL между поиском и чтением.
        fresh_ids = set(await allowed_chunk_ids(self.db, user))
        if public_only:
            fresh_ids.intersection_update(await externally_shareable_chunk_ids(self.db))
        ordered = [item for item in ordered if item in fresh_ids]
        async with self.db.connection() as conn:
            rows = await conn.fetch(
                """SELECT c.id,c.body,c.kind,c.page_number,d.id AS document_id,d.title,d.source_url,
                          d.classification,v.sha256,v.fetched_at
                   FROM chunks c JOIN document_versions v ON v.id=c.version_id
                   JOIN documents d ON d.id=v.document_id WHERE c.id=ANY($1::uuid[])""",
                ordered,
            )
        by_id = {row["id"]: dict(row) for row in rows}
        candidates = [by_id[item] for item in ordered if item in by_id]
        if candidates and self.settings.app_mode == "airgap":
            reranker = await self._reranker()
            relevance = await asyncio.to_thread(
                reranker.predict,
                [(query, item["body"][:1800]) for item in candidates],
            )
            candidates = [item for score, item in sorted(zip(relevance, candidates), key=lambda pair: float(pair[0]), reverse=True)
                          if not getattr(reranker, "outputs_probability", False)
                          or float(score) >= self.settings.qwen_rerank_min_probability]
        return candidates[:limit]

    async def rank_evidence(self, query: str, candidates: list[dict], limit: int = 18) -> list[dict]:
        """Rerank the union before truncation, using only permitted candidates."""
        if not candidates:
            return []
        if self.settings.app_mode == "airgap":
            reranker = await self._reranker()
            scores = await asyncio.to_thread(
                reranker.predict, [(query, item["text"]) for item in candidates],
            )
        else:
            # Connected mode needs no additional outbound call or local model weights.
            terms = set(re.findall(r"\w+", query.casefold()))
            scores = [len(terms & set(re.findall(r"\w+", (item["title"] + " " + item["text"]).casefold())))
                      for item in candidates]
        ranked = sorted(zip(scores, candidates), key=lambda pair: (float(pair[0]), pair[1]["chunk_id"]), reverse=True)
        if self.settings.app_mode == "airgap" and getattr(reranker, "outputs_probability", False):
            ranked = [(score, item) for score, item in ranked if float(score) >= self.settings.qwen_rerank_min_probability]
        return [item for _, item in ranked[:limit]]
