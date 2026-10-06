from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.access import allowed_chunk_ids, allowed_document_ids, require_research
from backend.auth import (
    CSRF_COOKIE, SESSION_COOKIE, LoginRequest, UserCreate, authenticate, bootstrap_admin,
    create_session, current_user, password_hasher, require_role, set_session_cookies, token_hash,
)
from backend.config import get_settings, is_local_model_endpoint
from backend.document_reader import preview_pages, readable_document
from backend.db import Database
from backend.ingestion import ingest_document
from backend.guardrails import contains_pii
from backend.models import ModelGateway
from backend.packages import export_public_package, import_public_package
from backend.retrieval import Retriever
from backend.report_pdf import render_report_pdf
from backend.secrets import load_openbao_secrets
from backend.telemetry import configure_tracing, instrument_api
from backend.vision import describe_document_images
from backend.jobs import enqueue_run
from backend.audit import install_access_audit, record_denial


class ResearchCreate(BaseModel):
    question: str = Field(min_length=10, max_length=4000)
    title: str = Field(min_length=3, max_length=200)
    classification: str = Field(default="public", pattern="^(public|restricted)$")


class ResearchFollowup(BaseModel):
    message: str = Field(min_length=3, max_length=4000)


class DomainCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    title: str = Field(min_length=2, max_length=120)


class DomainGrant(BaseModel):
    role: str = Field(pattern="^(reader|researcher|manager)$")


class DomainAssignment(BaseModel):
    domain_id: UUID


class PackageExport(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_openbao_secrets()
    get_settings.cache_clear()
    settings = get_settings()
    configure_tracing("research-api")
    db = Database(settings)
    await db.start()
    await bootstrap_admin(db, settings.bootstrap_email, settings.bootstrap_password)
    app.state.db = db
    app.state.settings = settings
    app.state.retriever = Retriever(db, settings)
    yield
    await app.state.retriever.close()
    await db.stop()


app = FastAPI(title="Защищённый исследователь", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
instrument_api(app)
install_access_audit(app)


@app.get("/health/live")
async def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok", "mode": request.app.state.settings.app_mode}


@app.post("/api/v1/auth/login")
async def login(payload: LoginRequest, response: Response, request: Request) -> dict:
    user = await authenticate(request.app.state.db, payload.email, payload.password)
    token, csrf = await create_session(request.app.state.db, user["id"])
    set_session_cookies(response, token, csrf, request.app.state.settings.app_mode == "airgap")
    return {"id": str(user["id"]), "email": user["email"], "role": user["role"]}


@app.post("/api/v1/auth/logout")
async def logout(response: Response, request: Request, user: dict = Depends(current_user)) -> dict:
    async with request.app.state.db.connection() as conn:
        await conn.execute("UPDATE sessions SET revoked_at=now() WHERE token_hash=$1", token_hash(request.cookies[SESSION_COOKIE]))
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"status": "ok"}


@app.get("/api/v1/auth/me")
async def me(user: dict = Depends(current_user)) -> dict:
    return {"id": str(user["id"]), "email": user["email"], "role": user["role"]}


@app.post("/api/v1/admin/users")
async def create_user(payload: UserCreate, request: Request, user: dict = Depends(require_role("admin"))) -> dict:
    async with request.app.state.db.connection() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO users(email,password_hash,role) VALUES($1,$2,$3) RETURNING id,email,role",
                payload.email.lower(), password_hasher.hash(payload.password), payload.role,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь уже существует") from exc
    return {"id": str(row["id"]), "email": row["email"], "role": row["role"]}


@app.get("/api/v1/admin/users")
async def list_users(request: Request, user: dict = Depends(require_role("admin"))) -> list[dict]:
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch("SELECT id,email,role FROM users ORDER BY email LIMIT 200")
    return [{"id": str(row["id"]), "email": row["email"], "role": row["role"]} for row in rows]


@app.get("/api/v1/capabilities")
async def capabilities(request: Request, user: dict = Depends(current_user)) -> dict:
    settings = request.app.state.settings
    local = is_local_model_endpoint(settings.model_base_url)
    return {"app_mode": settings.app_mode, "local_model": local,
            "internet_search": settings.app_mode == "connected" and settings.internet_search_enabled,
            "restricted_research": local, "max_document_bytes": settings.max_document_bytes}


@app.get("/api/v1/domains")
async def list_domains(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch(
            """SELECT d.id,d.code,d.title,d.classification,m.role
               FROM domains d LEFT JOIN domain_members m ON m.domain_id=d.id AND m.user_id=$1
               WHERE d.classification='public' OR m.user_id IS NOT NULL OR $2='admin'
               ORDER BY d.code""", user["id"], user["role"],
        )
    return [{**dict(row), "id": str(row["id"])} for row in rows]


@app.post("/api/v1/admin/domains")
async def create_domain(payload: DomainCreate, request: Request, user: dict = Depends(require_role("admin"))) -> dict:
    if payload.code == "public" or payload.code.startswith("private-"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Зарезервированный код домена")
    async with request.app.state.db.connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """INSERT INTO domains(code,title,classification) VALUES($1,$2,'restricted')
                   ON CONFLICT(code) DO NOTHING RETURNING id""", payload.code, payload.title,
            )
            if not row:
                raise HTTPException(status.HTTP_409_CONFLICT, "Домен уже существует")
            await conn.execute("INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,'manager')", row["id"], user["id"])
            await conn.execute("INSERT INTO audit_log(actor_id,action,target,decision) VALUES($1,'domain_create',$2,'allow')", user["id"], str(row["id"]))
    return {"id": str(row["id"]), "code": payload.code}


@app.get("/api/v1/admin/domains/{domain_id}/members")
async def list_domain_members(domain_id: UUID, request: Request, user: dict = Depends(require_role("admin"))) -> list[dict]:
    async with request.app.state.db.connection() as conn:
        domain = await conn.fetchrow("SELECT code,classification FROM domains WHERE id=$1", domain_id)
        if not domain or domain["classification"] != "restricted" or domain["code"].startswith("private-"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Общее пространство не найдено")
        rows = await conn.fetch("""SELECT u.id,u.email,m.role FROM domain_members m JOIN users u ON u.id=m.user_id
                                   WHERE m.domain_id=$1 ORDER BY u.email""", domain_id)
    return [{"id": str(row["id"]), "email": row["email"], "role": row["role"]} for row in rows]


@app.put("/api/v1/admin/domains/{domain_id}/members/{member_id}")
async def grant_domain(domain_id: UUID, member_id: UUID, payload: DomainGrant, request: Request, user: dict = Depends(require_role("admin"))) -> dict:
    async with request.app.state.db.connection() as conn:
        async with conn.transaction():
            domain = await conn.fetchrow("SELECT code,classification FROM domains WHERE id=$1", domain_id)
            member = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", member_id)
            if not domain or not member:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Домен или пользователь не найден")
            if domain["classification"] != "restricted" or domain["code"].startswith("private-"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Состав этого домена не изменяется")
            await conn.execute(
                """INSERT INTO domain_members(domain_id,user_id,role) VALUES($1,$2,$3)
                   ON CONFLICT(domain_id,user_id) DO UPDATE SET role=EXCLUDED.role""",
                domain_id, member_id, payload.role,
            )
            await conn.execute("INSERT INTO audit_log(actor_id,action,target,decision) VALUES($1,'domain_grant',$2,'allow')", user["id"], f"{domain_id}:{member_id}")
    return {"status": "ok"}


@app.delete("/api/v1/admin/domains/{domain_id}/members/{member_id}")
async def revoke_domain(domain_id: UUID, member_id: UUID, request: Request, user: dict = Depends(require_role("admin"))) -> dict:
    async with request.app.state.db.connection() as conn:
        async with conn.transaction():
            domain = await conn.fetchrow("SELECT code,classification FROM domains WHERE id=$1", domain_id)
            if not domain:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Домен не найден")
            if domain["classification"] != "restricted" or domain["code"].startswith("private-"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Состав этого домена не изменяется")
            await conn.execute("DELETE FROM domain_members WHERE domain_id=$1 AND user_id=$2", domain_id, member_id)
            await conn.execute("INSERT INTO audit_log(actor_id,action,target,decision) VALUES($1,'domain_revoke',$2,'allow')", user["id"], f"{domain_id}:{member_id}")
    return {"status": "ok"}


@app.put("/api/v1/admin/documents/{document_id}/domain")
async def assign_document_domain(document_id: UUID, payload: DomainAssignment, request: Request, user: dict = Depends(require_role("admin"))) -> dict:
    async with request.app.state.db.connection() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT d.classification AS document_class,dm.classification AS domain_class,dm.code
                   FROM documents d CROSS JOIN domains dm WHERE d.id=$1 AND dm.id=$2""",
                document_id, payload.domain_id,
            )
            if not row:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ или домен не найден")
            if row["document_class"] != row["domain_class"] or row["code"].startswith("private-"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Документ можно перенести только в подходящий общий домен")
            await conn.execute("UPDATE documents SET domain_id=$2 WHERE id=$1", document_id, payload.domain_id)
            await conn.execute("INSERT INTO audit_log(actor_id,action,target,decision) VALUES($1,'domain_assign',$2,'allow')", user["id"], str(document_id))
    return {"status": "ok"}


@app.post("/api/v1/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    classification: str = "restricted",
    domain_id: UUID | None = None,
    user: dict = Depends(require_role("admin", "researcher")),
) -> dict:
    data = await file.read(request.app.state.settings.max_document_bytes + 1)
    try:
        document_id = await ingest_document(
            request.app.state.db, request.app.state.settings, user["id"],
            file.filename or "Документ", data, file.content_type or "",
            "upload", classification=classification, domain_id=domain_id,
        )
        await request.app.state.retriever.index_document(document_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"id": str(document_id)}


@app.get("/api/v1/documents")
async def list_documents(request: Request, user: dict = Depends(current_user), q: str = "", classification: str = "", domain_id: UUID | None = None, offset: int = 0, limit: int = 50) -> list[dict]:
    if offset < 0 or not 1 <= limit <= 200 or classification not in {"", "public", "restricted"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректные параметры списка")
    allowed = await allowed_document_ids(request.app.state.db, user)
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch("""SELECT d.id,d.owner_id,d.domain_id,d.title,d.source_url,d.classification,d.created_at,
                                dm.title AS domain_title,dm.code AS domain_code,m.role AS domain_role
                                FROM documents d LEFT JOIN domains dm ON dm.id=d.domain_id
                                LEFT JOIN domain_members m ON m.domain_id=d.domain_id AND m.user_id=$6
                                WHERE d.id=ANY($1::uuid[]) AND ($2='' OR d.title ILIKE '%'||$2||'%')
                                AND ($3='' OR d.classification=$3) AND ($4::uuid IS NULL OR d.domain_id=$4)
                                ORDER BY d.created_at DESC,d.id DESC OFFSET $5 LIMIT $7""",
                                allowed, q[:200], classification, domain_id, offset, user["id"], limit)
    return [{**dict(row), "id": str(row["id"]), "domain_id": str(row["domain_id"]) if row["domain_id"] else None,
             "owner_id": str(row["owner_id"]) if row["owner_id"] else None, "created_at": row["created_at"].isoformat(),
             "can_analyze": user["role"] in {"admin", "researcher"} and (row["owner_id"] == user["id"] or row["domain_role"] in {"researcher", "manager"}),
             "can_export": user["role"] == "admin" and row["classification"] == "public"} for row in rows]


@app.post("/api/v1/documents/{document_id}/analyze-images")
async def analyze_document_images(document_id: UUID, request: Request, user: dict = Depends(require_role("admin", "researcher"))) -> dict:
    db = request.app.state.db
    async with db.connection() as conn:
        row = await conn.fetchrow("SELECT owner_id,classification,title,domain_id FROM documents WHERE id=$1", document_id)
        domain_role = await conn.fetchval("SELECT role FROM domain_members WHERE domain_id=$1 AND user_id=$2", row["domain_id"], user["id"]) if row else None
    if not row or document_id not in await allowed_document_ids(db, user) or (row["owner_id"] != user["id"] and domain_role not in {"researcher", "manager"}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Документ не найден")
    settings = request.app.state.settings
    try:
        count = await describe_document_images(db, settings, ModelGateway(settings), document_id, row["title"])
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if count:
        await request.app.state.retriever.index_document(document_id)
    return {"described_pages": count}


@app.get("/api/v1/documents/{document_id}/preview")
async def get_document_preview(document_id: UUID, request: Request, user: dict = Depends(current_user)) -> Response:
    row, chunks, path = await readable_document(document_id, request, user)
    pages = await run_in_threadpool(preview_pages, path, row["media_type"], chunks)
    payload = {key: row[key] for key in ("title", "classification", "source_url", "media_type", "sha256")}
    payload.update(id=str(document_id), pages=pages)
    return Response(content=json.dumps(payload, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/v1/documents/{document_id}/file")
async def get_document_file(document_id: UUID, request: Request, user: dict = Depends(current_user)) -> FileResponse:
    row, _, path = await readable_document(document_id, request, user)
    return FileResponse(
        path, media_type=row["media_type"], filename=f"document-{document_id}{path.suffix}",
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "sandbox; default-src 'none'"},
    )


@app.post("/api/v1/packages/export")
async def export_package(payload: PackageExport, request: Request, user: dict = Depends(require_role("admin"))) -> Response:
    try:
        data = await export_public_package(request.app.state.db, payload.document_ids)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="research-sources.zip"'},
    )


@app.post("/api/v1/packages/import")
async def import_package(request: Request, file: UploadFile = File(...), user: dict = Depends(require_role("admin"))) -> dict:
    data = await file.read(100 * 1024 * 1024 + 1)
    try:
        document_ids = await import_public_package(request.app.state.db, request.app.state.settings, request.app.state.retriever, data)
    except (ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Недопустимый пакет источников") from exc
    return {"document_ids": [str(value) for value in document_ids]}


@app.post("/api/v1/researches")
async def create_research(payload: ResearchCreate, request: Request, user: dict = Depends(require_role("admin", "researcher"))) -> dict:
    db: Database = request.app.state.db
    settings = request.app.state.settings
    if payload.classification == "restricted" and not is_local_model_endpoint(settings.model_base_url):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Для закрытого исследования настройте локальную модель")
    if not is_local_model_endpoint(settings.model_base_url) and contains_pii(payload.question):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Удалите персональные данные из публичного запроса к внешней модели")
    async with db.connection() as conn:
        async with conn.transaction():
            research_id = await conn.fetchval(
                "INSERT INTO researches(owner_id,title,question,classification) VALUES($1,$2,$3,$4) RETURNING id",
                user["id"], payload.title, payload.question, payload.classification,
            )
            await conn.execute("INSERT INTO messages(research_id,author,body) VALUES($1,'user',$2)", research_id, payload.question)
            await enqueue_run(conn, research_id, payload.question)
    return {"id": str(research_id), "status": "queued"}


@app.get("/api/v1/researches")
async def list_researches(request: Request, user: dict = Depends(current_user), q: str = "", status_filter: str = "", offset: int = 0, limit: int = 50) -> list[dict]:
    if offset < 0 or not 1 <= limit <= 100 or status_filter not in {"", "queued", "running", "awaiting_clarification", "completed", "failed", "cancelled"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректные параметры списка")
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch(
            """SELECT id,title,status,classification,updated_at FROM researches WHERE owner_id=$1
               AND ($2='' OR title ILIKE '%'||$2||'%') AND ($3='' OR status=$3)
               ORDER BY updated_at DESC,id DESC OFFSET $4 LIMIT $5""",
            user["id"], q[:200], status_filter, offset, limit,
        )
    return [{**dict(row), "id": str(row["id"]), "updated_at": row["updated_at"].isoformat()} for row in rows]


@app.get("/api/v1/researches/{research_id}")
async def get_research(research_id: UUID, request: Request, user: dict = Depends(current_user)) -> dict:
    db = request.app.state.db
    research = await require_research(db, research_id, user)
    async with db.connection() as conn:
        messages = await conn.fetch("SELECT author,body,created_at FROM messages WHERE research_id=$1 ORDER BY created_at", research_id)
        report = await conn.fetchrow("SELECT version,body,review_status,dropped_claims FROM reports WHERE research_id=$1 ORDER BY version DESC LIMIT 1", research_id)
        evidence_ids = await conn.fetch("SELECT chunk_id FROM evidence WHERE research_id=$1", research_id)
    permitted = set(await allowed_chunk_ids(db, user))
    report_unavailable = bool(report and any(row["chunk_id"] not in permitted for row in evidence_ids))
    if report_unavailable:
        report = None
    report_data = dict(report) if report else None
    if report_data and report_data["review_status"] is None:
        body = report_data["body"]
        report_data["review_status"] = "insufficient" if "Проверка выявила недостаток доказательств; выводы требуют дополнительной проверки." in body else None
        match = re.search(r"Исключено неподтверждённых формулировок: (\d+)\.", body)
        report_data["dropped_claims"] = int(match.group(1)) if match else None
    return {
        "id": str(research_id), "title": research["title"], "question": research["question"],
        "status": research["status"], "error": research["error"], "classification": research["classification"],
        "messages": [{"author": row["author"], "body": row["body"], "created_at": row["created_at"].isoformat()} for row in messages],
        "report": report_data, "report_unavailable_reason": "access_changed" if report_unavailable else None,
    }


@app.get("/api/v1/researches/{research_id}/event-history")
async def research_event_history(research_id: UUID, request: Request, after: int = 0, limit: int = 100, user: dict = Depends(current_user)) -> list[dict]:
    await require_research(request.app.state.db, research_id, user)
    if after < 0 or not 1 <= limit <= 200:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректный курсор")
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch("SELECT id,run_id,kind,payload,created_at FROM research_events WHERE research_id=$1 AND id>$2 ORDER BY id LIMIT $3", research_id, after, limit)
    return [{"id": row["id"], "run_id": str(row["run_id"]) if row["run_id"] else None, "kind": row["kind"],
             "payload": json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
             "created_at": row["created_at"].isoformat()} for row in rows]


@app.get("/api/v1/researches/{research_id}/sources")
async def research_sources(research_id: UUID, request: Request, user: dict = Depends(current_user)) -> list[dict]:
    await require_research(request.app.state.db, research_id, user)
    permitted = set(await allowed_chunk_ids(request.app.state.db, user))
    async with request.app.state.db.connection() as conn:
        rows = await conn.fetch("""SELECT e.note AS marker,c.id AS chunk_id,c.page_number,left(c.body,1200) AS excerpt,d.id AS document_id,
                                d.title,d.source_url,v.sha256 FROM evidence e JOIN chunks c ON c.id=e.chunk_id
                                JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
                                WHERE e.research_id=$1 ORDER BY substring(e.note from 2)::integer""", research_id)
    return [{"marker": row["marker"], "document_id": str(row["document_id"]), "title": row["title"],
             "page": row["page_number"], "excerpt": row["excerpt"], "source_url": row["source_url"], "sha256": row["sha256"]}
            for row in rows if row["chunk_id"] in permitted]


@app.get("/api/v1/researches/{research_id}/report.md")
async def download_report(research_id: UUID, request: Request, user: dict = Depends(current_user)) -> Response:
    research = await get_research(research_id, request, user)
    report = research["report"]
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Доступной версии отчёта нет")
    return Response(
        report["body"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="research-{research_id}.md"', "Cache-Control": "no-store"},
    )


@app.get("/api/v1/researches/{research_id}/report.pdf")
async def download_report_pdf(research_id: UUID, request: Request, user: dict = Depends(current_user)) -> Response:
    research = await get_research(research_id, request, user)
    report = research["report"]
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Доступной версии отчёта нет")
    try:
        content = await run_in_threadpool(render_report_pdf, research["title"], report["body"])
    except ValueError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    current = await get_research(research_id, request, user)
    if current["report"] is None or current["report"]["version"] != report["version"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Доступ к версии отчёта изменился")
    return Response(
        content, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="research-{research_id}.pdf"',
                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/v1/researches/{research_id}/messages")
async def followup(research_id: UUID, payload: ResearchFollowup, request: Request, user: dict = Depends(require_role("admin", "researcher"))) -> dict:
    db = request.app.state.db
    research = await require_research(db, research_id, user)
    if research["owner_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Только владелец может продолжить исследование")
    if research["status"] not in {"completed", "failed", "cancelled", "awaiting_clarification"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Исследование ещё выполняется")
    if not is_local_model_endpoint(request.app.state.settings.model_base_url) and contains_pii(payload.message):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Удалите персональные данные из уточнения к внешней модели")
    async with db.connection() as conn:
        async with conn.transaction():
            current = await conn.fetchrow("SELECT status FROM researches WHERE id=$1 FOR UPDATE", research_id)
            if current["status"] not in {"completed", "failed", "cancelled", "awaiting_clarification"}:
                raise HTTPException(status.HTTP_409_CONFLICT, "Исследование ещё выполняется")
            await conn.execute("INSERT INTO messages(research_id,author,body) VALUES($1,'user',$2)", research_id, payload.message)
            await conn.execute("UPDATE researches SET question=question || E'\nУточнение: ' || $2,status='queued',updated_at=now() WHERE id=$1", research_id, payload.message)
            question = await conn.fetchval("SELECT question FROM researches WHERE id=$1", research_id)
            await enqueue_run(conn, research_id, question)
    return {"status": "queued"}


@app.post("/api/v1/researches/{research_id}/cancel")
async def cancel(research_id: UUID, request: Request, user: dict = Depends(require_role("admin", "researcher"))) -> dict:
    research = await require_research(request.app.state.db, research_id, user)
    if research["owner_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Только владелец может отменить исследование")
    async with request.app.state.db.connection() as conn:
        async with conn.transaction():
            await conn.fetchval("SELECT id FROM researches WHERE id=$1 FOR UPDATE", research_id)
            await conn.execute("UPDATE research_runs SET status='cancelled' WHERE id=(SELECT run_id FROM research_jobs WHERE research_id=$1) AND status IN ('queued','running')", research_id)
            await conn.execute("UPDATE research_jobs SET status='cancelled',lease_token=NULL,locked_at=NULL WHERE research_id=$1 AND status IN ('queued','running')", research_id)
            await conn.execute("UPDATE researches SET status='cancelled',updated_at=now() WHERE id=$1", research_id)
    return {"status": "cancelled"}


@app.get("/api/v1/researches/{research_id}/events")
async def stream_events(research_id: UUID, request: Request, user: dict = Depends(current_user)) -> StreamingResponse:
    await require_research(request.app.state.db, research_id, user)
    try:
        last = int(request.headers.get("last-event-id") or request.query_params.get("after", "0"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректный курсор") from exc
    if last < 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректный курсор")

    async def stream():
        nonlocal last
        while not await request.is_disconnected():
            try:
                await current_user(request)
                await require_research(request.app.state.db, research_id, user)
            except HTTPException as exc:
                await record_denial(request, exc.status_code)
                return
            async with request.app.state.db.connection() as conn:
                rows = await conn.fetch("SELECT id,kind,payload FROM research_events WHERE research_id=$1 AND id>$2 ORDER BY id LIMIT 100", research_id, last)
            for row in rows:
                last = row["id"]
                payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                yield f"id: {last}\nevent: {row['kind']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
