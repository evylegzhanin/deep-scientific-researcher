from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher
from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from backend.db import Database

password_hasher = PasswordHasher()
SESSION_COOKIE = "research_session"
CSRF_COOKIE = "research_csrf"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=12)
    role: str = Field(pattern="^(admin|researcher|reader)$")


async def bootstrap_admin(db: Database, email: str, password: str) -> None:
    if not password:
        return
    if len(password) < 12:
        raise RuntimeError("BOOTSTRAP_PASSWORD должен содержать не менее 12 символов")
    async with db.connection() as conn:
        existing = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users)")
        if not existing:
            await conn.execute(
                "INSERT INTO users(email, password_hash, role) VALUES($1,$2,'admin')",
                email.lower(),
                password_hasher.hash(password),
            )


async def authenticate(db: Database, email: str, password: str) -> dict:
    async with db.connection() as conn:
        row = await conn.fetchrow("SELECT id,email,password_hash,role FROM users WHERE email=$1", email.lower())
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверные учётные данные")
    try:
        password_hasher.verify(row["password_hash"], password)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверные учётные данные") from None
    return dict(row)


async def create_session(db: Database, user_id: UUID) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO sessions(user_id,token_hash,csrf_hash,expires_at) VALUES($1,$2,$3,$4)",
            user_id,
            token_hash(token),
            token_hash(csrf),
            datetime.now(timezone.utc) + timedelta(hours=12),
        )
    return token, csrf


def set_session_cookies(response: Response, token: str, csrf: str, secure: bool) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=43200, httponly=True, secure=secure, samesite="strict")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=43200, httponly=False, secure=secure, samesite="strict")


async def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    db: Database = request.app.state.db
    async with db.connection() as conn:
        row = await conn.fetchrow(
            """SELECT u.id,u.email,u.role,s.csrf_hash
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at>now()""",
            token_hash(token),
        )
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия истекла")
    request.state.actor_id = row["id"]
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Неверный CSRF-токен")
        if not secrets.compare_digest(token_hash(csrf_header), row["csrf_hash"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Неверный CSRF-токен")
    return dict(row)


def require_role(*roles: str):
    async def dependency(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
        return user

    return dependency
