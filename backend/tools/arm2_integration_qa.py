"""Reproduce I06/I08 on newly created QA databases and a real file-backed provider.

Run from the repository root with the backend environment. TEST_DATABASE_URL supplies
only the local PostgreSQL account; its database is never changed. ARM2_QA_PG_BIN must
point to pg_dump/pg_restore/pg_ctl. Artifacts and credentials stay in ignored .local.
No existing database is stamped, restored over, or dropped.
"""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def require(value, message):
    if not value:
        raise AssertionError(message)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(args, **kwargs):
    if Path(args[0]).stem == 'pg_ctl':
        # Windows postmaster inherits pipe handles; wait for pg_ctl itself, not EOF from its server.
        return subprocess.run([str(a) for a in args], check=True, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **kwargs)
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, **kwargs)


def migrate(url, revision, *, downgrade=False):
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    settings.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    (command.downgrade if downgrade else command.upgrade)(cfg, revision)


def snapshot(url):
    engine = create_engine(url)
    try:
        with engine.connect() as db:
            names = db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                                    "AND (tablename LIKE 'arm2_%' OR tablename='users') ORDER BY tablename")).scalars()
            return {name: db.execute(text(f'SELECT row_to_json(t)::text FROM "{name}" t ORDER BY 1')).scalars().all()
                    for name in names}
    finally:
        engine.dispose()


def file_manifest(path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir()) if p.is_file()}


class Server:
    def __init__(self, url, directory, provider, signing, number):
        self.url, self.directory, self.provider = url, directory, provider
        self.base = f"http://127.0.0.1:{number}"
        self.env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False),
                    "APP_ENV": "test", "COOKIE_SECURE": "false", "APP_ORIGIN": self.base,
                    "DATA_DIR": str(directory / "runtime"), "ARM2_QA_FILE_ROOT": str(provider),
                    "ARM2_DOCUMENT_DISK_ROOT": "ARM2_INT_QA/files", "ARM2_TABLE_SIGNING_KEY": signing,
                    "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "tools")]), "PYTHONUTF8": "1"}
        self.proc = None

    def start(self):
        self.log = (self.directory / "backend.log").open("ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "arm2_qa_server:app", "--host", "127.0.0.1",
                                      "--port", self.base.rsplit(":", 1)[1]], cwd=REPO, env=self.env,
                                     stdout=self.log, stderr=self.log)
        for _ in range(150):
            require(self.proc.poll() is None, "QA backend exited; inspect its local log")
            try:
                if httpx.get(self.base + "/api/auth/me").status_code in (200, 401, 403):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        raise RuntimeError("QA backend readiness timed out")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(20)
            self.log.close()
            self.proc = None

    @contextmanager
    def login(self, password):
        with httpx.Client(base_url=self.base, timeout=30) as c:
            request(c, "POST", "/api/auth/login", json={"login": "integration-qa", "password": password})
            c.headers["X-CSRF-Token"] = c.cookies["csrf"]
            yield c


def request(c, method, path, status=200, **kwargs):
    r = c.request(method, path, **kwargs)
    require(r.status_code == status, f"{method} {path}: {r.status_code} {r.text[:500]}")
    return r.json()


def stage(c, url, content, name):
    item = request(c, "POST", url + "/stages", 201, json={"original_upload_name": name, "size": len(content)})
    request(c, "PUT", url + "/stages/" + item["id"] + "/content", content=content)
    r = c.get(url + "/stages/" + item["id"] + "/preview")
    require(r.status_code == 200 and r.content == content, "Stage preview differs")
    return {"stage_id": item["id"]}


def verify(c, state, files):
    result = {"vehicle": request(c, "GET", "/api/arm2/vehicles/" + state["vehicle"]),
              "bundle": request(c, "GET", state["document"] + "/files"),
              "vehicle_history": request(c, "GET", "/api/arm2/history/VEH/" + state["vehicle"]),
              "document_history": request(c, "GET", "/api/arm2/history/DOCUMENT/" + state["document"].split('/')[-1])}
    for revision in result["bundle"]["revisions"]:
        for item in revision["files"]:
            r = c.get(state["document"] + f"/revisions/{revision['id']}/files/{item['id']}/download")
            require(r.status_code == 200 and r.content == files[item['sha256']], "Historical FILE bytes differ")
    if state.get("workspace"):
        result["type"] = request(c, "GET", "/api/arm2/types/" + state["type"])
        result["workspace"] = request(c, "GET", "/api/arm2/bulk/sessions/" + state["workspace"])
        result["workspace"].pop("preview_id")
        result["protocols"] = request(c, "GET", "/api/arm2/bulk/protocols")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-pg-data", required=True)
    parser.add_argument("--browser", action="store_true", help="Also verify restored native UI using ARM2_QA_PLAYWRIGHT")
    args = parser.parse_args()
    source = make_url(os.environ["TEST_DATABASE_URL"])
    require(source.host in ("127.0.0.1", "localhost") and source.database.endswith("_test")
            and source.database != "contractor_max_test", "Only local disposable QA accounts allowed")
    pg_data = Path(args.restart_pg_data).resolve()
    require(pg_data == REPO / ".local" / "pg-data", "Can restart only this clone's own PostgreSQL")
    pg_bin = Path(os.environ["ARM2_QA_PG_BIN"])
    work = REPO / ".local" / ("integration-" + uuid4().hex)
    work.mkdir(parents=True)
    files_root = work / "provider"
    files_root.mkdir()
    signing, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    clean_name = "arm2_int_" + uuid4().hex + "_clean_test"
    clean_url = source.set(database=clean_name)
    names = ["arm2_int_" + uuid4().hex + suffix for suffix in ("_b3_test", "_upgrade_test", "_restore_test")]
    urls = [source.set(database=name) for name in names]
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        for name in [clean_name, *names]:
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin.dispose()
    pg_env = {**os.environ, "PGHOST": source.host, "PGPORT": str(source.port), "PGUSER": source.username,
              "PGPASSWORD": source.password or ""}
    cfg = Config(str(ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(cfg)
    require(scripts.get_heads() == ["0068"], "Expected one integration head")
    require([(scripts.get_revision(r).revision, scripts.get_revision(r).down_revision)
             for r in ("0065", "0066", "0067", "0068")] ==
            [("0065", "0064"), ("0066", "0065"), ("0067", "0066"), ("0068", "0067")], "Wrong chain")
    migrate(clean_url, "0064")
    legacy = create_engine(clean_url)
    with legacy.begin() as db:
        db.execute(text("INSERT INTO arm2_vehicle_types(id,name,requires_sts,requires_calibration,requires_wialon,status,version) "
                        "VALUES('TYPE-700001','Legacy integration',NULL,false,false,'CONFIRMED',4)"))
        db.execute(text("INSERT INTO arm2_vehicles(id,name,plate_number,type_id,accounting_state,version) "
                        "VALUES('VEH-700001','Legacy integration','А111АА86','TYPE-700001','Действующая',7)"))
    legacy.dispose()
    migrate(clean_url, "0068")
    # Clean QA upgrade is real. At this stage the only TYPE event is migration-0067.
    first_snapshot = snapshot(clean_url)
    require('"version":5' in first_snapshot['arm2_vehicle_types'][0], "Legacy TYPE version lost")
    clean_rows = [json.loads(row) for row in first_snapshot['arm2_vehicle_types']]
    require([(r['id'], r['requires_sts'], r['plate_category'], r['status'], r['version']) for r in clean_rows] ==
            [('TYPE-700001', None, None, 'REVIEW', 5)], 'Clean migration guessed or lost TYPE values')
    require([(json.loads(row)['type_id'], json.loads(row)['version']) for row in first_snapshot['arm2_vehicles']] ==
            [('TYPE-700001', 7)], 'Clean migration changed VEH links/versions')
    require(len(first_snapshot['arm2_history_events']) == 1 and
            json.loads(first_snapshot['arm2_history_events'][0])['actor_id'] == 'migration-0067', 'Missing migration audit')
    migration_event = json.loads(first_snapshot['arm2_history_events'][0])
    require((migration_event['correlation_id'], migration_event['source'], migration_event['version_before'],
             migration_event['version_after']) == ('migration-0067', 'arm2-migration', 4, 5), 'Wrong TYPE audit provenance')
    # Independently build an actual B3-only 0066 database, never downgrade an integration snapshot into it.
    migrate(urls[0], '0066')
    os.environ['DATABASE_URL'] = urls[0].render_as_string(hide_password=False)
    settings.cache_clear()
    from sqlalchemy.orm import Session

    from app.auth import hasher
    from app.models import User
    engine = create_engine(urls[0])
    with Session(engine) as db:
        db.add(User(login="integration-qa", name="Synthetic QA", role="ADMIN_OPERATOR", active=True,
                    password_hash=hasher.hash(password), permissions=[]))
        db.commit()
    engine.dispose()
    server = Server(urls[0], work, files_root, signing, port())
    contents = [b"%PDF-1.4\nQA first\n%%EOF\n", b"%PDF-1.4\nQA second\n%%EOF\n"]
    file_bytes = {hashlib.sha256(value).hexdigest(): value for value in contents}
    frontend = None
    try:
        server.start()
        with server.login(password) as c:
            veh = request(c, "POST", "/api/arm2/vehicles", 201, json={"name": "Integration QA", "plate_number": "A123BC86"})
            cp = request(c, "POST", "/api/arm2/counterparties", 201, json={"name": "Synthetic CP"})
            org = request(c, "POST", "/api/arm2/organizations", 201, json={"name": "Synthetic ORG"})
            kind = request(c, "POST", "/api/arm2/document-kinds", 201,
                           json={"name": "Integration files", "applicable_object_types": ["VEH"], "max_current_documents": None})
            base = f"/api/arm2/veh/{veh['id']}/documents"
            doc = request(c, "POST", base, 201, json={"kind_ref": kind["id"], "name": "Integration document"})
            state = {"vehicle": veh["id"], "document": base + "/" + doc["id"]}
            selections = [stage(c, state["document"], value, f"original-{i}.pdf") for i, value in enumerate(contents)]
            request(c, "POST", state["document"] + "/accept",
                    json={"expected_version": 1, "idempotency_key": str(uuid4()), "files": selections})
            bundle = request(c, "GET", state["document"] + "/files")
            request(c, "POST", state["document"] + "/accept", json={"expected_version": 2, "idempotency_key": str(uuid4()),
                    "files": [{"file_id": bundle["revisions"][0]["files"][0]["id"]}]})
            expected_b3 = verify(c, state, file_bytes)
        server.stop()
        schema = create_engine(urls[0])
        with schema.connect() as db:
            require(db.scalar(text("SELECT version_num FROM alembic_version")) == '0066', "Wrong B3 snapshot")
            require(db.scalar(text("SELECT to_regclass('arm2_document_revision_contexts')")) is not None, "Not B3 0066")
            require(db.scalar(text("SELECT to_regclass('arm2_bulk_sessions')")) is None, "Old B4 numbering detected")
        schema.dispose()
        b3_snapshot = snapshot(urls[0])
        run([pg_bin / "pg_dump", "-Fc", "-f", work / "b3-0066.dump", names[0]], env=pg_env)
        run([pg_bin / "pg_restore", "--exit-on-error", "--no-owner", "-d", names[1], work / "b3-0066.dump"], env=pg_env)
        require(snapshot(urls[1]) == b3_snapshot, "B3 restore changed rows/history")
        migrate(urls[1], "0068")
        server = Server(urls[1], work, files_root, signing, port())
        server.start()
        with server.login(password) as c:
            require(verify(c, state, file_bytes) == expected_b3, "B3 API/files changed on 0066->0068")
            typ = request(c, "POST", "/api/arm2/types", 201, json={"name": "Confirmed QA", "requires_sts": True,
                "requires_calibration": False, "requires_wialon": False, "plate_category": "Автомобильный"})
            request(c, "POST", f"/api/arm2/vehicles/{veh['id']}/operational-owner", json={"expected_version": 1,
                "expected_owner_kind": None, "expected_owner_id": None, "target_kind": "CP", "target_id": cp['id'],
                "confirm_reassignment": False})
            request(c, "POST", f"/api/arm2/vehicles/{veh['id']}/operational-owner", json={"expected_version": 2,
                "expected_owner_kind": "CP", "expected_owner_id": cp['id'], "target_kind": "ORG", "target_id": org['id'],
                "confirm_reassignment": True})
            w = request(c, "POST", "/api/arm2/bulk/sessions?filename=integration.tsv", 201,
                        content=f"id\tГосномер\tTYPE — ID\tМодель\n{veh['id']}\t{veh['plate_number']}\t{typ['id']}\tQA imported".encode())
            apply = {"command_id": str(uuid4()), "preview_id": w['preview_id'], "row_key": w['plans'][0]['key'],
                     "fields": {"type_id": 0, "model": 0}}
            saved = request(c, "POST", f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply)
            state.update(type=typ['id'], workspace=w['id'])
            # Reopen records fresh preview state before the database/file backup.
            refreshed = verify(c, state, file_bytes)
            require(refreshed['workspace']['approval_reset'], 'Saving must invalidate the previous comparison')
            expected = verify(c, state, file_bytes)
            require(not expected['workspace']['approval_reset'], 'Reopened comparison did not settle')
            require(expected['workspace']['results'][0]['status'] == 'SAVED', "Receipt not verified")
        server.stop()
        source_snapshot = snapshot(urls[1])
        manifest = file_manifest(files_root)
        require(len(manifest) == 2, "Expected both current and historical FILE bytes")
        run([pg_bin / "pg_dump", "-Fc", "-f", work / "integration.dump", names[1]], env=pg_env)
        backup_files = work / "provider-backup"
        shutil.copytree(files_root, backup_files)
        restore_files = work / "provider-restored"
        shutil.copytree(backup_files, restore_files)
        run([pg_bin / "pg_restore", "--exit-on-error", "--no-owner", "-d", names[2], work / "integration.dump"], env=pg_env)
        # Restart only the isolated, path-checked PostgreSQL instance, with backend stopped.
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-m', 'fast', 'stop'])
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-l', work / 'postgres-restart.log', 'start'])
        require(snapshot(urls[2]) == source_snapshot, "Integration restore changed DB rows/history")
        require(file_manifest(restore_files) == manifest, "File-backed provider restore differs")
        restored_engine = create_engine(urls[2])
        with restored_engine.connect() as db:
            require(db.scalar(text("SELECT version_num FROM alembic_version")) == '0068', 'Restored head differs')
            fk_count = db.scalar(text("SELECT count(*) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace"))
            require(fk_count > 0 and db.scalar(text("SELECT count(*) FROM pg_constraint WHERE contype='f' "
                        "AND connamespace='public'::regnamespace AND NOT convalidated")) == 0, 'Unvalidated foreign keys')
        restored_engine.dispose()
        frontend_port = port()
        frontend_origin = f'http://127.0.0.1:{frontend_port}'
        server = Server(urls[2], work, restore_files, signing, port())
        server.env['APP_ORIGIN'] = frontend_origin
        server.start()
        frontend_log = (work / 'frontend.log').open('ab')
        frontend_command = ['node', str(REPO / 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1',
                            '--port', str(frontend_port), '--strictPort']
        for cycle in range(2):
            frontend = subprocess.Popen(frontend_command, cwd=REPO / 'frontend', stdout=frontend_log, stderr=frontend_log,
                                        env={**os.environ, 'VITE_API_TARGET': server.base})
            for _attempt in range(100):
                try:
                    page = httpx.get(f'http://127.0.0.1:{frontend_port}/arm2/veh')
                    if page.status_code == 200 and '<html' in page.text:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('QA frontend readiness timed out')
            if cycle == 0:
                frontend.terminate()
                frontend.wait(20)
                frontend = None
        if args.browser:
            browser_state = {**state, 'password': password, 'origin': frontend_origin, 'directory': str(work),
                             'historical_sha256': hashlib.sha256(contents[1]).hexdigest()}
            browser_result = subprocess.run(['node', str(ROOT / 'tools/arm2_integration_browser.mjs')],
                env={**os.environ, 'ARM2_QA_BROWSER_STATE': json.dumps(browser_state)}, capture_output=True)
            (work / 'browser.log').write_bytes(browser_result.stdout + browser_result.stderr)
            require(browser_result.returncode == 0, f'Native browser checks failed; inspect {work / "browser.log"}')
        frontend.terminate()
        frontend.wait(20)
        frontend = None
        frontend_log.close()
        with server.login(password) as c:
            reopened = verify(c, state, file_bytes)
            (work / 'expected-api.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding='utf-8')
            (work / 'restored-api.json').write_text(json.dumps(reopened, ensure_ascii=False, indent=2), encoding='utf-8')
            require(reopened == expected, "Reopened API differs after all restarts/restore")
            require(request(c, 'POST', f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply) == saved,
                    'Duplicate command after restore was not idempotent')
        # B4 protected downgrade must leave every row and the exact head unchanged.
        before_guard = snapshot(urls[2])
        try:
            migrate(urls[2], '0066', downgrade=True)
        except RuntimeError as error:
            require('downgrade refused' in str(error), 'Unexpected downgrade failure')
        else:
            raise AssertionError('Destructive downgrade was allowed')
        require(snapshot(urls[2]) == before_guard, 'Protected downgrade changed data')
        report = {"classification": "AUTHOR-RUN, synthetic QA; not UAT or independent acceptance", "I06": "PASS", "I08": "PASS",
                  "head": "0068", "chain": ["0064", "0065 B3", "0066 B3", "0067 TYPE", "0068 B4"],
                  "databases": [clean_name, *names], "file_manifest": manifest, "file_backup_restored": True,
                  "db_snapshot_equal": True, "backend_postgres_frontend_restarted": True,
                  "reopened_api_equal": True, "duplicate_receipt_equal": True, "protected_downgrade": True,
                  "native_ui_verified": args.browser,
                  "validated_foreign_keys": fk_count, "clean_legacy_type": clean_rows[0], "type_migration_event": migration_event,
                  "snapshot_sha256": hashlib.sha256(json.dumps(source_snapshot, sort_keys=True).encode()).hexdigest(),
                  "observed": expected, "dump_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                         for p in work.glob('*.dump')}}
        (work / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({"I06": "PASS", "I08": "PASS", "evidence": str(work / 'result.json')}))
    finally:
        server.stop()
        if frontend is not None:
            frontend.terminate()
            frontend.wait(20)


if __name__ == '__main__':
    main()
