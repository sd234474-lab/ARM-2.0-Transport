"""Disposable SQL + FILE/runtime restore, never the VM or a real cloud account."""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_arm2_calibration_approval import letter, ok, prepare
from test_arm2_document_api import api as api
from test_arm2_document_file_api import FakeDisk, pdf
from test_arm2_document_file_api import qa as qa

from app.arm2 import document_file_storage as files
from app.config import settings
from app.db import get_db


def backup_clients(db):
    major = int(db.scalar(text("SHOW server_version_num"))) // 10000
    db.rollback()
    explicit = os.environ.get("ARM2_TEST_PG_BIN")
    candidates = [Path(explicit)] if explicit else [Path("/usr/lib/postgresql") / str(major) / "bin"]
    if not explicit and shutil.which("pg_dump"):
        candidates.append(Path(shutil.which("pg_dump")).parent)
    suffix = ".exe" if os.name == "nt" else ""
    found = False
    for directory in candidates:
        pair = [directory / (name + suffix) for name in ["pg_dump", "pg_restore"]]
        if not all(path.is_file() for path in pair):
            continue
        found = True
        versions = [
            subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True).stdout
            for path in pair
        ]
        # A newer pg_restore can issue SET commands unavailable on an older target.
        # Use matching clients; never strip SQL or ignore restore errors.
        if all(re.search(r"PostgreSQL\)\s+" + str(major) + r"\.", version) for version in versions):
            return tuple(str(path) for path in pair)
    if not found and not explicit and os.environ.get("CI") not in {"true", "1"}:
        pytest.skip("PostgreSQL backup clients required for disposable restore evidence")
    pytest.fail(
        f"Install matching PostgreSQL {major} pg_dump/pg_restore clients; restore checks remain mandatory"
    )


def test_sql_and_file_runtime_restore_reopens_evidence_and_resumes_private_reply(qa, monkeypatch, tmp_path):
    c, db, user, parents, kind, fake = qa
    dump, restore = backup_clients(db)
    vehicles = prepare(qa)
    _, response = letter(c, vehicles)
    request_id = ok(response, 201)["request_id"]
    path = "/api/arm2/approvals/" + request_id
    initial = ok(c.get(path))
    stage_id = str(uuid.uuid4())
    ok(c.put(path + f"/response-stages/{stage_id}?name=answer.pdf", content=pdf("restore reply")))
    source = db.get_bind().url
    assert source.database.startswith("arm2_") and source.database.endswith("_test")
    target_name = "arm2_restore_" + uuid.uuid4().hex + "_test"
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    restored = None
    sql_backup = tmp_path / "snapshot.dump"
    file_backup = tmp_path / "disk-backup"
    runtime_backup = tmp_path / "runtime-backup"
    env = {
        **os.environ,
        "PGHOST": source.host,
        "PGPORT": str(source.port or 5432),
        "PGUSER": source.username,
        "PGPASSWORD": source.password,
    }
    db.commit()
    tables = ['arm2_document_kinds', 'arm2_vehicles', 'arm2_document_files', 'arm2_history_events']
    preserved = {table: db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all()
                 for table in tables}
    db.rollback()
    result = subprocess.run(
        [
            dump,
            "--no-owner",
            "--no-privileges",
            "--format=custom",
            "--file",
            str(sql_backup),
            "--dbname",
            source.database,
        ],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0, "Disposable SQL dump failed; private diagnostic suppressed"
    shutil.copytree(fake.path, file_backup)
    shutil.copytree(files.zone(), runtime_backup)
    try:
        with admin.connect() as connection:
            assert not connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": target_name}
            )
            connection.exec_driver_sql('CREATE DATABASE "' + target_name + '"')
        result = subprocess.run(
            [
                restore,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--dbname",
                target_name,
                str(sql_backup),
            ],
            env=env,
            capture_output=True,
        )
        assert result.returncode == 0, "Disposable SQL restore failed; private diagnostic suppressed"
        restored = create_engine(source.set(database=target_name))
        provider = FakeDisk(tmp_path / "restored-disk")
        shutil.copytree(file_backup, provider.path, dirs_exist_ok=True)
        new_runtime = tmp_path / "restored-data"
        shutil.copytree(runtime_backup, new_runtime / "arm2-document-runtime-v1")
        monkeypatch.setenv("DATA_DIR", str(new_runtime))
        settings.cache_clear()
        monkeypatch.setattr(files, "storage", lambda: provider)
        # Reuse the same app/API/auth/CSRF with an isolated restored SQL connection.
        with Session(restored) as restored_db:
            c.app.dependency_overrides[get_db] = lambda: restored_db
            try:
                reopened = ok(c.get(path))
                assert reopened == initial
                assert restored_db.scalar(text("SELECT version_num FROM alembic_version")) == "0074"
                assert {table: restored_db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all()
                        for table in tables} == preserved
                assert c.get(path + f"/files/{initial['letter_file_id']}/download").content.startswith(
                    b"%PDF-"
                )
                assert c.get(path + f"/response-stages/{stage_id}/preview").content == pdf("restore reply")
                command = {
                    "command_id": str(uuid.uuid4()),
                    "stage_id": stage_id,
                    "expected_version": initial["version"],
                    "decisions": [
                        {"vehicle_id": d["vehicle_id"], "expected_version": d["version"], "state": "APPROVED"}
                        for d in initial["decisions"]
                    ],
                }
                ok(c.post(path + "/response", json=command))
                accepted = ok(c.get(path))
                assert all(d["state"] == "APPROVED" for d in accepted["decisions"])
                assert c.get(path + f"/files/{accepted['response_file_ids'][0]}/download").content == pdf(
                    "restore reply"
                )
                assert ok(c.get("/api/arm2/history/APPROVAL/" + request_id))
            finally:
                c.app.dependency_overrides.pop(get_db, None)
    finally:
        if restored:
            restored.dispose()
        with admin.connect() as connection:
            assert target_name.startswith("arm2_restore_") and target_name.endswith("_test")
            connection.exec_driver_sql('DROP DATABASE IF EXISTS "' + target_name + '"')
        admin.dispose()
