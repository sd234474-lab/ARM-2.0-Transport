"""Scoped native ARM2 controller. All writes/processes belong to one new root."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
from policy import API_PORT, APPROVED_HEAD, DB_NAME, DB_PORT, PACKAGE, ROOT, WEB_PORT  # noqa: E402

PG = ROOT / "runtime/pgsql/bin"
PYTHON = ROOT / "python/Scripts/python.exe"
HERE = ROOT / "source/deploy/arm2-windows-native"
STATE = ROOT / "private/processes.json"


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value, *, new=False):
    with Path(path).open("x" if new else "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)


def env():
    private = read_json(ROOT / "private/secrets.json")
    result = {key: value for key, value in os.environ.items()
              if not key.upper().startswith("PG") and key.upper() not in {"DATABASE_URL", "PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    result.update(DATABASE_URL=f"postgresql+psycopg://arm2_uat:{private['db_password']}@127.0.0.1:{DB_PORT}/{DB_NAME}",
                  APP_ENV="test", APP_ORIGIN=f"http://127.0.0.1:{WEB_PORT}", COOKIE_SECURE="false", ORGANIZATIONS_ENABLED="false",
                  DATA_DIR=str(ROOT / "data"), MAX_BOT_TOKEN="", MAX_WEBHOOK_SECRET=private["inert_secret"],
                  MAX_UPDATE_MODE="webhook", YANDEX_DISK_TOKEN="", YANDEX_MAPS_API_KEY="", ML_AUTO_TRAIN="false", ML_DOWNLOAD_BACKBONE="false",
                  PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                  PYTHONPATH=os.pathsep.join(map(str, [ROOT / "source/backend", HERE, ROOT / "source/deploy/arm2-windows-uat"])))
    result["PATH"] = os.pathsep.join([str(PG), str(PYTHON.parent), os.path.join(os.environ["SystemRoot"], "System32")])
    return result


def require_root():
    import stat

    if HERE.resolve() != Path(__file__).resolve().parent or ROOT.resolve() != ROOT:
        raise RuntimeError("Controller must run from its own dedicated installed UAT root")
    for path in [ROOT, *ROOT.parents]:
        if path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("UAT ancestor is a reparse point")


def call(arguments, **kwargs):
    return subprocess.run([str(value) for value in arguments], env=env(), check=True,
                          creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)


def available(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def wait_http(port, seconds=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                result = json.load(response)
            identity = read_json(ROOT / "private/instance.json")
            if result.get("instance_id") != identity["instance_id"] or result.get("package") != PACKAGE or result.get("migration") != "0061":
                raise RuntimeError("Health is from another instance/schema")
            return result
        except (urllib.error.URLError, TimeoutError):
            time.sleep(.2)
    raise RuntimeError("Own UAT health not ready; inspect only own logs")


def secure_private():
    directory = ROOT / "private"
    directory.mkdir(exist_ok=False)
    # Per-directory ACL only. No system policy/registry/credential-store changes.
    script = "$p=$env:ARM2_PRIVATE;$a=Get-Acl -LiteralPath $p;$a.SetAccessRuleProtection($true,$false);$u=[Security.Principal.WindowsIdentity]::GetCurrent().User;$r=[Security.AccessControl.FileSystemAccessRule]::new($u,'FullControl','ContainerInherit,ObjectInherit','None','Allow');$a.SetAccessRule($r);Set-Acl -LiteralPath $p -AclObject $a"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=os.environ | {"ARM2_PRIVATE": str(directory)}, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def pg_owned():
    import psutil

    pidfile = ROOT / "db-data/postmaster.pid"
    if not pidfile.exists():
        return False
    rows = pidfile.read_text().splitlines()
    process = psutil.Process(int(rows[0]))
    args = process.cmdline()
    own_data = (ROOT / "db-data").resolve()
    if (Path(process.exe()).resolve() != (PG / "postgres.exe").resolve()
            or Path(rows[1]).resolve() != own_data or rows[3] != str(DB_PORT)
            or abs(process.create_time() - int(rows[2])) > 2
            or "-D" not in args or args.index("-D") + 1 >= len(args)
            or Path(args[args.index("-D") + 1]).resolve() != own_data):
        raise RuntimeError("Postmaster identity mismatch; no stop/start attempted")
    return True


def start_pg():
    if pg_owned():
        return
    available(DB_PORT)
    call([PG / "pg_ctl.exe", "-D", ROOT / "db-data", "-l", ROOT / "logs/postgres.log", "-w", "-t", "30",
          "-o", f"-h 127.0.0.1 -p {DB_PORT}", "start"])
    if not pg_owned():
        raise RuntimeError("New PostgreSQL did not start")


def init():
    if (ROOT / "private").exists() or (ROOT / "db-data").exists():
        raise RuntimeError("Refusing reinitialization; use start/health")
    for port in (DB_PORT, API_PORT, WEB_PORT):
        available(port)
    secure_private()
    for directory in ("data", "logs", "evidence"):
        (ROOT / directory).mkdir(exist_ok=True)
    write_json(ROOT / "private/instance.json", {"root": str(ROOT), "instance_id": secrets.token_hex(16),
               "package": PACKAGE, "approved_head": APPROVED_HEAD}, new=True)
    secret = {"db_password": secrets.token_hex(32), "inert_secret": secrets.token_hex(32),
              "admin_login": "uat-admin", "admin_password": secrets.token_urlsafe(24)}
    write_json(ROOT / "private/secrets.json", secret, new=True)
    pwfile = ROOT / "private/initdb-password.txt"
    pwfile.write_text(secret["db_password"] + "\n", encoding="ascii")
    call([PG / "initdb.exe", "-D", ROOT / "db-data", "-U", "arm2_uat", "-A", "scram-sha-256",
          "--pwfile", pwfile, "--encoding=UTF8", "--locale=C"])
    pwfile.unlink()  # only our own temporary password file inside the checked root
    start_pg()
    call([PYTHON, HERE / "run.py", "migrate"], cwd=ROOT / "source/backend")
    write_json(STATE, {}, new=True)
    for action in ("START", "STOP", "HEALTH"):
        filename = ROOT / f"{action}_ARM2.cmd"
        text = f'@echo off\r\n"%~dp0tools\\bootstrap\\Scripts\\python.exe" -X utf8 "%~dp0source\\deploy\\arm2-windows-native\\run.py" {action.lower()}\r\n'
        with filename.open("x", encoding="ascii", newline="") as stream:
            stream.write(text)
    print("New native UAT DB/schema/admin initialized; local credentials are private, not printed")


def migrate():
    from policy import deny_external_network, validate_environment

    validate_environment()
    deny_external_network()
    import psycopg

    private = read_json(ROOT / "private/secrets.json")
    with psycopg.connect(host="127.0.0.1", port=DB_PORT, user="arm2_uat", password=private["db_password"],
                         dbname="postgres", autocommit=True, connect_timeout=5) as db:
        if Path(db.execute("SHOW data_directory").fetchone()[0]).resolve() != (ROOT / "db-data").resolve():
            raise RuntimeError("Not the new UAT cluster")
        if db.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,)).fetchone():
            raise RuntimeError("UAT database already exists; no migration of unknown data")
        db.execute("CREATE DATABASE arm2_win_run_001")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "source/backend/alembic.ini")), "0061")
    # Use the existing local admin command and its password rules/audit.
    import getpass

    from app.cli import main as existing_cli

    original_getpass = getpass.getpass
    try:
        getpass.getpass = lambda *_args, **_kwargs: private["admin_password"]
        sys.argv = ["app.cli", "create-admin", "--login", "uat-admin", "--name", "ARM2 NATIVE UAT ADMIN"]
        existing_cli()
    finally:
        getpass.getpass = original_getpass


def spawn(role):
    import psutil

    with (ROOT / f"logs/{role}.log").open("ab", buffering=0) as output:
        process = subprocess.Popen([str(PYTHON), "-X", "utf8", str(HERE / "run.py"), role], cwd=ROOT / "source/backend",
                                   env=env(), stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    return {"pid": process.pid, "created": psutil.Process(process.pid).create_time()}


def owned_process(record, role):
    import psutil

    try:
        process = psutil.Process(record["pid"])
    except psutil.NoSuchProcess:
        return None
    arguments = process.cmdline()
    if abs(process.create_time() - record["created"]) > .01 or str(HERE / "run.py") not in arguments or arguments[-1] != role:
        raise RuntimeError("PID reused or process ownership mismatch; no process stopped")
    return process


def is_own_console(child, parent):
    # CREATE_NO_WINDOW on this Windows creates one private conhost child.
    # Observe its natural exit; never terminate console hosts by name.
    args = child.cmdline()
    return (Path(child.exe()).resolve() == (Path(os.environ["SystemRoot"]) / "System32/conhost.exe").resolve()
            and child.ppid() == parent.pid and child.create_time() >= parent.create_time()
            and len(args) == 2 and args[1] == "0x4")


def start():
    state = read_json(STATE)
    for role in ("api", "web"):
        if role in state and owned_process(state[role], role):
            raise RuntimeError("UAT already started; use health/stop, not a second start")
    for port in (API_PORT, WEB_PORT):
        available(port)
    start_pg()
    state["api"] = spawn("api")
    write_json(STATE, state)
    wait_http(API_PORT)
    state["web"] = spawn("web")
    write_json(STATE, state)
    print(json.dumps(wait_http(WEB_PORT), ensure_ascii=False))


def stop():
    import psutil

    state = read_json(STATE)
    for role in ("web", "api"):
        if role not in state:
            continue
        process = owned_process(state[role], role)
        if process:
            children = process.children(recursive=True)
            python_children = []
            for child in children:
                if is_own_console(child, process):
                    continue
                args = child.cmdline()
                if str(HERE / "run.py") not in args or args[-1] != role:
                    raise RuntimeError("Unknown child process; no broad shutdown")
                python_children.append(child)
            for child in reversed(python_children):
                child.terminate()
            process.terminate()
            _, alive = psutil.wait_procs(children + [process], timeout=8)
            if alive:
                raise RuntimeError("Own process not stopped; no kill by name/port permitted")
    write_json(STATE, {})
    if pg_owned():
        call([PG / "pg_ctl.exe", "-D", ROOT / "db-data", "-m", "fast", "-w", "-t", "30", "stop"])
    print("Only native ARM2 UAT processes/cluster stopped; data retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "migrate", "start", "stop", "health", "verify", "api", "web"])
    action = parser.parse_args().action
    require_root()
    if action == "health":
        print(json.dumps(wait_http(WEB_PORT, seconds=3), ensure_ascii=False))
    elif action == "verify":
        call([PYTHON, HERE / "verify-db.py"])
        print("Browser record/history and four DB signer FK rejections: PASS")
    elif action == "api":
        import uvicorn

        uvicorn.run("native_app:app", host="127.0.0.1", port=API_PORT, access_log=True)
    elif action == "web":
        from web import main as web_main

        web_main()
    else:
        globals()[action]()


if __name__ == "__main__":
    main()
