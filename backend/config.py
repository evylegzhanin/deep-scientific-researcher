from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LOCAL_MODEL_HOSTS = {"localhost", "127.0.0.1", "model", "vision", "gpu-private"}


def is_local_model_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOCAL_MODEL_HOSTS and not parsed.username and not parsed.password


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: Literal["connected", "airgap"] = "connected"
    database_url: str = "postgresql://research:research-dev-only@localhost:5434/research"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "research_chunks"
    graph_retrieval_enabled: bool = False
    qwen_rerank_min_probability: float = Field(default=0.1, ge=0, le=1)
    file_root: Path = Path("data/files")
    pdf_parser: Literal["auto", "basic", "docling"] = "auto"
    docling_artifacts_path: Path | None = None
    model_base_url: str = "https://openrouter.ai/api/v1"
    vision_base_url: str | None = None
    model_api_key: str = ""
    text_model: str = "qwen/qwen3.8-27b"
    vision_model: str = "qwen/qwen3.8-27b"
    model_max_output_tokens: int | None = Field(default=None, ge=128, le=32768)
    model_request_timeout_seconds: int = Field(default=90, ge=1, le=3600)
    model_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    rerank_model: str = "Qwen/Qwen3-Reranker-0.6B"
    openrouter_embedding_model: str = "baai/bge-m3"
    frontend_origin: str = "http://localhost:5173"
    bootstrap_email: str = "admin@example.local"
    bootstrap_password: str = ""
    max_document_bytes: int = 25 * 1024 * 1024
    max_research_steps: int = 3
    max_sources: int = 20
    allow_external_images: bool = False
    internet_search_enabled: bool = True
    research_timeout_seconds: int = 3600

    @model_validator(mode="after")
    def check_airgap(self) -> "Settings":
        if self.app_mode == "airgap":
            for url in (self.model_base_url, self.vision_base_url or self.model_base_url):
                parsed = urlparse(url)
                if not is_local_model_endpoint(url) or parsed.fragment:
                    raise ValueError("Air-gapped режим требует локальные endpoint моделей")
            if self.pdf_parser == "docling" and self.docling_artifacts_path is None:
                raise ValueError("Docling в закрытом контуре требует локальный путь к моделям")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
