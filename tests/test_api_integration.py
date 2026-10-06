from __future__ import annotations

import importlib
import os
from uuid import uuid4

import pytest
import psycopg
from fastapi.testclient import TestClient


@pytest.mark.skipif(not os.getenv("INTEGRATION_TESTS"), reason="Требуется локальный PostgreSQL")
def test_login_csrf_and_research_isolation(monkeypatch) -> None:
    admin_email = f"api-admin-{uuid4()}@test.local"
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "temporary-test-pass-123")
    monkeypatch.setenv("BOOTSTRAP_EMAIL", admin_email)
    monkeypatch.setenv("OPENBAO_URL", "")
    from backend.config import get_settings
    from backend.auth import password_hasher

    test_url = os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research")
    monkeypatch.setenv("DATABASE_URL", test_url)
    with psycopg.connect(test_url) as conn:
        conn.execute("INSERT INTO users(email,password_hash,role) VALUES(%s,%s,'admin')",
                     (admin_email, password_hasher.hash("temporary-test-pass-123")))

    get_settings.cache_clear()
    app = importlib.import_module("backend.api").app
    with TestClient(app) as admin:
        assert admin.get("/health/ready").status_code == 200
        assert admin.get("/api/v1/auth/me").status_code == 401
        assert admin.post("/api/v1/auth/login", json={"email": admin_email, "password": "wrong"}).status_code == 401
        login = admin.post("/api/v1/auth/login", json={"email": admin_email, "password": "temporary-test-pass-123"})
        assert login.status_code == 200
        assert admin.get("/api/v1/auth/me").json()["role"] == "admin"
        assert admin.post("/api/v1/researches", json={"title": "Проверка", "question": "Как устроена система контроля доступа?"}).status_code == 403
        admin.headers["X-CSRF-Token"] = admin.cookies.get("research_csrf")
        other_email = f"reader-{uuid4()}@test.local"
        created = admin.post("/api/v1/admin/users", json={"email": other_email, "password": "reader-password-123", "role": "reader"})
        assert created.status_code == 200
        research = admin.post("/api/v1/researches", json={"title": "Проверка ACL", "question": "Как устроена система контроля доступа?"})
        assert research.status_code == 200
        research_id = research.json()["id"]
        assert admin.get(f"/api/v1/researches/{research_id}").status_code == 200
        assert admin.post("/api/v1/auth/logout").status_code == 200
        assert admin.get("/api/v1/auth/me").status_code == 401
    with TestClient(app) as reader:
        assert reader.post("/api/v1/auth/login", json={"email": other_email, "password": "reader-password-123"}).status_code == 200
        denied = reader.get(f"/api/v1/researches/{research_id}")
        assert denied.status_code == 404
        request_id = denied.headers['x-request-id']
    with psycopg.connect(os.getenv("TEST_DATABASE_URL", "postgresql://research:research-dev-only@localhost:5434/research")) as conn:
        audit = conn.execute("SELECT actor_id,action,target,decision FROM audit_log WHERE request_id=%s", (request_id,)).fetchone()
        assert audit and audit[0] is not None and audit[3] == 'deny'
        assert audit[1] == 'GET /api/v1/researches/{research_id}'
        assert research_id in audit[2]
        assert conn.execute("SELECT count(*) FROM audit_log WHERE decision='deny' AND actor_id IS NULL").fetchone()[0] > 0
        conn.execute("DELETE FROM researches WHERE id=%s", (research_id,))
        conn.execute("DELETE FROM users WHERE email=%s", (other_email,))
        conn.execute("DELETE FROM users WHERE email=%s", (admin_email,))
