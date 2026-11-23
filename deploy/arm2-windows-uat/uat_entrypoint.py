"""Deployment-only ASGI allowlist around the unchanged approved app."""

from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from uat_policy import allowed, readiness, validate_environment

validate_environment()

from app.api import app as approved_app  # noqa: E402
from app.db import engine  # noqa: E402


def health():
    with engine().connect() as connection:
        return readiness(connection)


async def app(scope, receive, send):
    if scope["type"] == "http":
        if not allowed(scope["method"], scope["path"]):
            return await JSONResponse({"detail": "Outside ARM2 UAT scope"}, status_code=404)(scope, receive, send)
        if scope["path"] == "/api/health":
            try:
                result = await run_in_threadpool(health)
            except (SQLAlchemyError, RuntimeError):
                return await JSONResponse({"ok": False, "detail": "UAT schema not ready"}, status_code=503)(scope, receive, send)
            return await JSONResponse(result)(scope, receive, send)
    return await approved_app(scope, receive, send)
