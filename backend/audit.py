"""Metadata-only denial audit. Never log raw paths, query strings or bodies."""
import logging
from uuid import UUID, uuid4

from fastapi import Request

logger = logging.getLogger(__name__)


async def record_denial(request: Request, status_code: int):
    request_id = getattr(request.state, 'request_id', None) or uuid4()
    route = request.scope.get('route')
    action = f"{request.method} {getattr(route, 'path', 'unmatched')}"
    targets = []
    for key, value in request.path_params.items():
        try:
            targets.append(f'{key}={UUID(str(value))}')
        except ValueError:
            pass
    try:
        async with request.app.state.db.connection() as conn:
            await conn.execute(
                """INSERT INTO audit_log(actor_id,action,target,decision,request_id)
                   VALUES($1,$2,$3,'deny',$4)""",
                getattr(request.state, 'actor_id', None), action,
                ','.join(targets) or 'none', request_id,
            )
    except Exception:
        # Preserve denial if storage fails; emit a metadata-only operator alert.
        logger.error('access_audit_write_failed request_id=%s status=%s', request_id, status_code)


def install_access_audit(app):
    @app.middleware('http')
    async def audit_denials(request: Request, call_next):
        request.state.request_id = uuid4()
        response = await call_next(request)
        response.headers['X-Request-ID'] = str(request.state.request_id)
        if response.status_code in {401, 403, 404}:
            await record_denial(request, response.status_code)
        return response
