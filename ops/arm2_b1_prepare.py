"""Prepare dependencies/fresh vendor runtime only in this checkout’s .local/b1. Windows QA, not deployment."""

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/b1"
LOCAL.mkdir(parents=True, exist_ok=True)
env = {
    k: v
    for k, v in os.environ.items()
    if k.upper()
    in {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "COMSPEC",
        "PATHEXT",
    }
}
env.update(
    PYTHONDONTWRITEBYTECODE="1",
    PYTHONUTF8="1",
    UV_CACHE_DIR=str(LOCAL / "cache/uv"),
    PIP_CACHE_DIR=str(LOCAL / "cache/pip"),
    npm_config_cache=str(LOCAL / "cache/npm"),
    UV_PROJECT_ENVIRONMENT=str(LOCAL / "python"),
    UV_LINK_MODE="copy",
)


def run(args, cwd=None):
    with (LOCAL / "setup.log").open("ab", buffering=0) as log:
        subprocess.run(
            list(map(str, args)),
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=log,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def dependencies():
    bootstrap = LOCAL / "bootstrap"
    run([sys.executable, "-m", "venv", bootstrap])
    run(
        [
            bootstrap / "Scripts/python.exe",
            "-m",
            "pip",
            "install",
            "uv==0.8.22",
            "psutil==7.0.0",
        ]
    )
    run(
        [
            bootstrap / "Scripts/uv.exe",
            "sync",
            "--frozen",
            "--python",
            sys.executable,
            "--no-install-project",
            "--project",
            ROOT / "backend",
        ]
    )
    run([shutil.which("npm.cmd"), "ci", "--no-audit", "--no-fund"], ROOT / "frontend")
    run(
        [
            shutil.which("npm.cmd"),
            "install",
            "--prefix",
            LOCAL / "browser",
            "--no-audit",
            "--no-fund",
            "playwright@1.64.1",
        ]
    )
    print("B1 own Python/dev/frontend/browser dependencies ready")


def runtime():
    downloads = LOCAL / "downloads"
    downloads.mkdir(exist_ok=True)
    archives = [
        (
            "postgresql.zip",
            "https://get.enterprisedb.com/postgresql/postgresql-17.11-1-windows-x64-binaries.zip",
            "6eabdf00d2893713b75db4336a23c3fdf505f056e217ec6e2e95d901750cfea3",
        ),
        (
            "postgis.zip",
            "https://download.osgeo.org/postgis/windows/pg17/postgis-bundle-pg17-3.6.2x64.zip",
            "7ba180ee2a352987b9a2f194673652c59483b55852295ccf401dceccd8765425",
        ),
    ]
    target = LOCAL / "runtime/pgsql"
    if target.exists():
        raise RuntimeError("Refusing runtime overwrite")
    for name, url, digest in archives:
        path = downloads / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            run(
                [
                    "curl.exe",
                    "--fail",
                    "--location",
                    "--continue-at",
                    "-",
                    "--max-time",
                    "900",
                    "--output",
                    path,
                    url,
                ]
            )
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        with zipfile.ZipFile(path) as archive:
            prefix = (
                "pgsql/"
                if name == "postgresql.zip"
                else next(
                    x
                    for x in archive.namelist()
                    if x.endswith("share/extension/postgis.control")
                )[: -len("share/extension/postgis.control")]
            )
            for member in archive.infolist():
                if not member.filename.startswith(prefix):
                    continue
                output = (target / member.filename[len(prefix) :]).resolve()
                assert output.is_relative_to(target.resolve())
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, output.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
    print("B1 separate vendor runtime verified and extracted")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["dependencies", "runtime"])
    globals()[parser.parse_args().phase]()
