"""Fail-closed package boundary; no application/auth/core changes."""

import os
import re

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SOURCE = "1f8c06d1f9269146ce7c07508b02dd09c89ff763"
PACKAGE = "ARM2-WIN-UAT-R2-001"
DATABASE = "arm2_uat_r2_001"
REVISION = "0061"


def validate_environment():
    try:
        url = make_url(os.environ.get("DATABASE_URL", ""))
    except (ArgumentError, ValueError, TypeError):
        raise RuntimeError("UAT database URL rejected (suppressed)") from None
    if (url.drivername, url.host, url.port, url.database, url.username) != (
        "postgresql+psycopg", "uat-db", 5432, DATABASE, "arm2_uat"
    ) or not re.fullmatch(r"[a-f0-9]{64}", url.password or "") or url.query:
        raise RuntimeError("UAT database identity/config rejected (connection string suppressed)")
    required = {
        "APP_ENV": "test", "APP_ORIGIN": "http://127.0.0.1:18082", "COOKIE_SECURE": "false",
        "ORGANIZATIONS_ENABLED": "false", "DATA_DIR": "/data", "MAX_BOT_TOKEN": "",
        "YANDEX_DISK_TOKEN": "", "MAX_UPDATE_MODE": "webhook",
        "ML_AUTO_TRAIN": "false", "ML_DOWNLOAD_BACKBONE": "false",
    }
    if any(os.environ.get(key) != value for key, value in required.items()):
        raise RuntimeError("UAT environment rejected; external integrations/organizations must be disabled")
    if not re.fullmatch(r"[a-f0-9]{64}", os.environ.get("MAX_WEBHOOK_SECRET", "")):
        raise RuntimeError("UAT inert secret must be generated locally")


def allowed(method, path):
    if re.fullmatch(r"/api/arm2/(types|vehicles|counterparties|organizations)/search", path):
        return method == "GET"
    if path == "/api/health":
        return method == "GET"
    if path in {"/api/auth/me", "/api/meta", "/api/platform/status"}:
        return method == "GET"
    if path in {"/api/auth/login", "/api/auth/logout"}:
        return method == "POST"
    if path == "/api/users":
        return method in {"GET", "POST"}
    if re.fullmatch(r"/api/users/[1-9][0-9]*", path):
        return method == "PATCH"
    if re.fullmatch(r"/api/arm2/(types|vehicles|counterparties|organizations)", path):
        return method in {"GET", "POST"}
    if re.fullmatch(r"/api/arm2/(types|vehicles|counterparties|organizations)/[A-Z]+-[0-9]+", path):
        return method in {"GET", "PUT"}
    if re.fullmatch(r"/api/arm2/(counterparties|organizations)/[A-Z]+-[0-9]+/representatives", path):
        return method in {"GET", "POST"}
    if re.fullmatch(r"/api/arm2/(counterparties|organizations)/[A-Z]+-[0-9]+/representatives/[A-Z]+-[0-9]+", path):
        return method in {"GET", "PUT"}
    if re.fullmatch(r"/api/arm2/history/(TYPE|VEH|CP|ORG|CPREP|ORGREP)/[A-Z]+-[0-9]+", path):
        return method == "GET"
    return method == "GET" and bool(re.fullmatch(r"/api/arm2/history/events/[1-9][0-9]*", path))


def readiness(connection):
    from sqlalchemy import text

    if connection.scalar(text("SELECT current_database()")) != DATABASE:
        raise RuntimeError("UAT database mismatch")
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != REVISION:
        raise RuntimeError("UAT schema is not at the approved revision")
    return {"ok": True, "package": PACKAGE, "source_sha": SOURCE, "database": DATABASE, "migration": REVISION}
