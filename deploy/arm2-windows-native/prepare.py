"""Prepare ONLY a fresh native ARM2 UAT instance; never install system services."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path("C:/ARM2-UAT/ARM2-WIN-RUN-001")
SOURCE = "6118252e8a43e8bf534012ba91284c20427ea5e0"
BUSINESS = "1f8c06d1f9269146ce7c07508b02dd09c89ff763"
DOWNLOADS = {
    "postgresql.zip": "https://get.enterprisedb.com/postgresql/postgresql-17.11-1-windows-x64-binaries.zip",
    "postgis.zip": "https://download.osgeo.org/postgis/windows/pg17/postgis-bundle-pg17-3.6.2x64.zip",
}


def checked_root():
    if ROOT.resolve() != ROOT or not ROOT.is_dir():
        raise RuntimeError("Expected new dedicated UAT directory")
    for path in [ROOT, *ROOT.parents]:
        if path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("UAT ancestor is a reparse point")
    return ROOT


def environment():
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", UV_CACHE_DIR=str(ROOT / "cache/uv"),
               PIP_CACHE_DIR=str(ROOT / "cache/pip"), npm_config_cache=str(ROOT / "cache/npm"),
               UV_PROJECT_ENVIRONMENT=str(ROOT / "python"), UV_LINK_MODE="copy")
    return env


def run(arguments, cwd=None):
    subprocess.run([str(value) for value in arguments], cwd=cwd, env=environment(), check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def protected_snapshot(catalog, pgdata):
    if catalog is None or pgdata is None:
        raise RuntimeError("Explicit existing catalog/PGDATA paths required for read-only protection snapshot")
    catalog, pgdata = Path(catalog), Path(pgdata)
    if not catalog.is_dir() or not (pgdata / "PG_VERSION").is_file():
        raise RuntimeError("Existing protection scope not found; no installation started")
    previous = catalog / "90_EVIDENCE/before.json"
    with previous.open(encoding="utf-8") as stream:
        files = {item["path"] for item in json.load(stream)["protected_files"]}
    for directory in [catalog, pgdata]:
        files.update(str(path) for path in directory.rglob("*") if path.is_file())
    result = []
    for name in sorted(files):
        path = Path(name)
        value = path.stat()
        result.append({"path": str(path), "bytes": value.st_size, "mtime_ns": value.st_mtime_ns, "sha256": hash_file(path)})
    output = ROOT / "evidence/protected-before.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"Protected {len(result)} existing files; contents/credentials not exported")


def extract(archive, prefix, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Archive path escape rejected")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def downloads(catalog=None, pgdata=None):
    if not (ROOT / "evidence/protected-before.json").exists():
        protected_snapshot(catalog, pgdata)
    metadata = {}
    for name, url in DOWNLOADS.items():
        target = ROOT / "downloads" / name
        if not target.exists() or target.stat().st_size == 0:
            run(["curl.exe", "--fail", "--location", "--retry", "1", "--max-time", "240", "--output", target, url])
        elif not zipfile.is_zipfile(target):
            with target.open("rb") as stream:
                if stream.read(4) != b"PK\x03\x04":
                    raise RuntimeError("Non-ZIP response retained; do not use as runtime")
            run(["curl.exe", "--fail", "--location", "--continue-at", "-", "--max-time", "240", "--output", target, url])
        if not zipfile.is_zipfile(target):
            raise RuntimeError("Incomplete download retained in own UAT downloads; not a usable archive")
        metadata[name] = {"url": url, "bytes": target.stat().st_size, "sha256": hash_file(target)}
        print(f"Downloaded {name}: {target.stat().st_size} bytes")
    pg = ROOT / "runtime/pgsql"
    if pg.exists():
        raise RuntimeError("Refusing to overwrite native runtime")
    extract(ROOT / "downloads/postgresql.zip", "pgsql/", pg)
    with zipfile.ZipFile(ROOT / "downloads/postgis.zip") as bundle:
        control = next(name for name in bundle.namelist() if name.endswith("share/extension/postgis.control"))
    extract(ROOT / "downloads/postgis.zip", control[:-len("share/extension/postgis.control")], pg)
    with (ROOT / "downloads/manifest.json").open("x", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2)
    run([pg / "bin/postgres.exe", "--version"])
    run([pg / "bin/pg_ctl.exe", "--version"])


def source():
    target = ROOT / "source"
    if target.exists():
        raise RuntimeError("Source already present; no overwrite")
    run(["git", "clone", "--single-branch", "--branch", "codex/arm-win-struct-001",
         "https://github.com/sd234474-lab/ARM-2.0-Transport.git", target])
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
    if actual != SOURCE:
        raise RuntimeError("Approved source HEAD mismatch; STOP")
    shutil.copytree(Path(__file__).resolve().parent, target / "deploy/arm2-windows-native",
                    ignore=shutil.ignore_patterns("__pycache__"))


def python():
    bootstrap = ROOT / "tools/bootstrap"
    if not bootstrap.exists():
        run([sys.executable, "-m", "venv", bootstrap])
    executable = bootstrap / "Scripts/python.exe"
    run([executable, "-m", "pip", "install", "--disable-pip-version-check", "uv==0.8.22", "psutil==7.0.0", "ruff==0.11.13"])
    run([bootstrap / "Scripts/uv.exe", "sync", "--frozen", "--no-dev", "--no-install-project",
         "--python", sys.executable, "--project", ROOT / "source/backend"])
    run([ROOT / "python/Scripts/python.exe", "-c", "import fastapi,uvicorn,psycopg,sqlalchemy,geoalchemy2,shapely,argon2; print('Required native API imports PASS')"])


def frontend():
    npm = shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("Existing system Node/npm required; no system installation permitted")
    run([npm, "ci", "--no-audit", "--no-fund"], ROOT / "source/frontend")
    run([npm, "run", "build"], ROOT / "source/frontend")
    browser = ROOT / "tools/browser"
    browser.mkdir(parents=True, exist_ok=True)
    run([npm, "install", "--prefix", browser, "--no-audit", "--no-fund", "playwright@1.64.1"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["downloads", "source", "python", "frontend"])
    parser.add_argument("--catalog-path", type=Path)
    parser.add_argument("--protected-pgdata", type=Path)
    args = parser.parse_args()
    checked_root()
    if args.phase == "downloads":
        for port in (18082, 18083, 55442):
            with socket.socket() as connection:
                connection.bind(("127.0.0.1", port))
    if args.phase == "downloads":
        downloads(args.catalog_path, args.protected_pgdata)
    else:
        globals()[args.phase]()


if __name__ == "__main__":
    main()
