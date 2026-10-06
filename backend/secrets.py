from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

SECRET_KEYS = {"DATABASE_URL", "MODEL_API_KEY", "BOOTSTRAP_PASSWORD"}


async def load_openbao_secrets() -> None:
    """Читает KV v2 из OpenBao до создания конфигурации приложения."""
    base_url = os.getenv("OPENBAO_URL", "")
    if not base_url:
        return
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "openbao"}):
        raise RuntimeError("OpenBao требует HTTPS вне локального стенда")
    token_file = os.getenv("OPENBAO_TOKEN_FILE", "")
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else os.getenv("OPENBAO_TOKEN", "")
    if not token:
        raise RuntimeError("Не задан токен OpenBao")
    path = os.getenv("OPENBAO_SECRET_PATH", "secret/data/research")
    if not path.startswith("secret/data/") or ".." in path:
        raise RuntimeError("Недопустимый путь OpenBao KV")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(f"{base_url.rstrip('/')}/v1/{path}", headers={"X-Vault-Token": token})
        response.raise_for_status()
    values = response.json()["data"]["data"]
    for key in SECRET_KEYS:
        if key in values:
            if not isinstance(values[key], str):
                raise RuntimeError("Значение секрета должно быть строкой")
            os.environ[key] = values[key]
