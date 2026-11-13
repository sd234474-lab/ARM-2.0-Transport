"""Native-instance boundary, applied before importing the unchanged application."""

import hashlib
import json
import os
import re
import socket
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("C:/ARM2-UAT/ARM2-WIN-RUN-001")
APP_SOURCE = "aaba19e5c363a8fee22eb6c49060d3452117bcd9"
APPROVED_HEAD = APP_SOURCE
ORIGIN_HEAD = "6118252e8a43e8bf534012ba91284c20427ea5e0"
PACKAGE = "ARM2-WIN-RUN-001"
DB_NAME = "arm2_win_run_001"
DB_PORT, API_PORT, WEB_PORT = 55442, 18083, 18082
RELEASE_FILES = {
    'deploy/arm2-windows-native/policy.py', 'deploy/arm2-windows-native/run.py',
    'deploy/arm2-windows-native/native_app.py', 'deploy/arm2-windows-native/web.py',
    'deploy/arm2-windows-uat/uat_policy.py', 'backend/app/arm2/read_routes.py', 'frontend/dist/index.html',
}


def config():
    with (ROOT / "private/instance.json").open(encoding="utf-8") as stream:
        result = json.load(stream)
    if (result["root"] != str(ROOT) or result["approved_head"] != APPROVED_HEAD
            or result.get("origin_head") != ORIGIN_HEAD or result.get("source_sha") != APP_SOURCE
            or result.get("package") != PACKAGE or not re.fullmatch(r"[a-f0-9]{32}", result.get("instance_id", ""))):
        raise RuntimeError("Native UAT identity/root mismatch")
    release = json.loads((ROOT / "private/release.json").read_text(encoding="utf-8"))
    if (release.get("source_sha") != APP_SOURCE or release.get("instance_id") != result["instance_id"]
            or not re.fullmatch(r"[a-f0-9]{40}", release.get("package_head", ""))
            or not RELEASE_FILES.issubset(release.get("files", {}))):
        raise RuntimeError("Native UAT release identity mismatch")
    for relative, expected in release["files"].items():
        path = (ROOT / "source" / relative).resolve()
        if not path.is_relative_to((ROOT / "source").resolve()) or not path.is_file():
            raise RuntimeError("Native UAT release path rejected")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Native UAT release file changed")
    result["package_head"] = release["package_head"]
    return result


def validate_environment():
    try:
        url = urlsplit(os.environ["DATABASE_URL"])
        valid = (url.scheme == "postgresql+psycopg" and url.hostname == "127.0.0.1"
                 and url.port == DB_PORT and url.path == "/" + DB_NAME
                 and url.username == "arm2_uat" and re.fullmatch(r"[a-f0-9]{64}", url.password or "")
                 and not url.query and not url.fragment)
    except (KeyError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError("Native UAT database config rejected (credentials suppressed)")
    expected = {"APP_ENV": "test", "APP_ORIGIN": f"http://127.0.0.1:{WEB_PORT}", "COOKIE_SECURE": "false",
                "ORGANIZATIONS_ENABLED": "false", "MAX_BOT_TOKEN": "", "YANDEX_DISK_TOKEN": "",
                "ML_AUTO_TRAIN": "false", "ML_DOWNLOAD_BACKBONE": "false", "MAX_UPDATE_MODE": "webhook",
                "DATA_DIR": str(ROOT / "data")}
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Native UAT must not inherit integrations/organizations/production data")
    if os.environ.get("YANDEX_MAPS_API_KEY", ""):
        raise RuntimeError("Native UAT maps integration must be disabled")
    config()


def allowed_destination(host, port, *, socketpair=False):
    return host in {"127.0.0.1", "::1"} and (port in {DB_PORT, API_PORT, WEB_PORT} or socketpair)


def deny_external_network():
    """Do not alter Windows firewall/PATH: block Python egress in this process.

    libpq uses only the strictly validated literal loopback DB URI. Windows
    asyncio's temporary socketpair may connect to its own ephemeral loopback port.
    """
    original_connect, original_ex = socket.socket.connect, socket.socket.connect_ex
    original_dns, original_pair = socket.getaddrinfo, socket.socketpair
    local = threading.local()

    def check(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6} and not allowed_destination(
            address[0], address[1], socketpair=getattr(local, "pair", False)
        ):
            raise OSError("External/non-UAT connection blocked by native UAT policy")

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_ex(sock, address)

    def dns(host, port, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", None}:
            raise OSError("External DNS blocked by native UAT policy")
        return original_dns(host, port, *args, **kwargs)

    def pair(*args, **kwargs):
        local.pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.pair = False

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.getaddrinfo, socket.socketpair = dns, pair


def readiness(connection):
    from sqlalchemy import text

    database = connection.scalar(text("SELECT current_database()"))
    directory = connection.scalar(text("SHOW data_directory"))
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    spatial = connection.scalar(text("SELECT postgis_lib_version()"))
    if database != DB_NAME or Path(directory).resolve() != (ROOT / "db-data").resolve() or revision != "0061":
        raise RuntimeError("Wrong native database/schema/data directory")
    identity = config()
    return {"ok": True, "package": PACKAGE, "instance_id": identity["instance_id"],
            "source_sha": APP_SOURCE, "approved_head": APPROVED_HEAD, "migration": revision,
            "package_head": identity["package_head"], "data_directory": str(ROOT / "db-data"),
            "database": database, "postgis": spatial, "url": f"http://127.0.0.1:{WEB_PORT}/arm2"}
