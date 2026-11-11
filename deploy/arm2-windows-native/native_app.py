"""Deployment wrapper; no worker, polling, scheduler, webhook or external API."""

from policy import deny_external_network, readiness, validate_environment
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

validate_environment()
deny_external_network()

from app.api import app as original  # noqa: E402
from app.db import engine  # noqa: E402
from uat_policy import allowed  # noqa: E402 - frozen deploy-only allowlist from PR38


def health():
    with engine().connect() as connection:
        return readiness(connection)


async def app(scope, receive, send):
    if scope["type"] == "http":
        if not allowed(scope["method"], scope["path"]):
            return await JSONResponse({"detail": "Outside native ARM2 UAT scope"}, status_code=404)(scope, receive, send)
        if scope["path"] == "/api/health":
            try:
                result = await run_in_threadpool(health)
            except (SQLAlchemyError, RuntimeError):
                return await JSONResponse({"ok": False, "detail": "Native UAT not ready"}, status_code=503)(scope, receive, send)
            return await JSONResponse(result)(scope, receive, send)
    return await original(scope, receive, send)
