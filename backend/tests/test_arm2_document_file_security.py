import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_arm2_calibration_approval import letter, prepare
from test_arm2_deletion import BASE, execute, mark, ok, preview, queue_snapshot
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa

from app.arm2 import background_operations
from app.arm2 import document_file_storage as storage
from app.arm2.approval_models import ApprovalAttempt
from app.arm2.deletion_cleanup import clean_job
from app.arm2.deletion_models import DeletionBatch, DeletionMark
from app.arm2.deletion_service import CLEANUP
from app.arm2.document_file_models import DocumentFile, DocumentRevision, FileStage
from app.arm2.document_file_store import cleanup_expired_staging, instant
from app.arm2.file_read_guard import (
    approval_attempt_key,
    attempt_lease,
    protected_background_work,
    stage_attempt_key,
    try_idle_attempt,
)
from app.arm2.history_models import HistoryEvent
from app.arm2.operation_models import BackgroundOperation
from app.auth import hasher
from app.models import User


def user_client(qa):
    c, db, *_ = qa
    user = User(
        login="files-second",
        name="files-second",
        role="OPERATOR",
        active=True,
        permissions=["arm2_cp_view", "arm2_cp_edit", "arm2_document_view", "arm2_document_edit"],
        password_hash=hasher.hash("file-qa-password"),
    )
    db.add(user)
    db.commit()
    client = TestClient(c.app)
    assert (
        client.post("/api/auth/login", json={"login": user.login, "password": "file-qa-password"}).status_code
        == 200
    )
    client.headers["X-CSRF-Token"] = client.cookies["csrf"]
    return client, user


def test_typed_rights_cross_parent_and_historical_file_urls_fail_closed(qa):
    c, db, *_ = qa
    url = create(qa)
    staged = stage(c, url, pdf())
    assert accept(c, url, 1, [staged]).status_code == 200
    revision = bundle(c, url)["revisions"][0]
    file_id = revision["files"][0]["id"]
    still_pending = stage(c, url, pdf("pending"))
    other, user = user_client(qa)
    with other:
        for grants in [
            [],
            ["arm2_cp_view"],
            ["arm2_document_view"],
            ["arm2_cp_edit", "arm2_document_edit"],
            ["arm2_org_view", "arm2_document_view"],
            ["arm2_doc_kind_view", "arm2_doc_kind_edit"],
        ]:
            user.permissions = grants
            db.commit()
            for path in [
                url + "/files",
                url + f"/revisions/{revision['id']}/files/{file_id}/download",
                "/api/arm2/history/DOCUMENT/" + url.split("/")[-1],
            ]:
                assert other.get(path).status_code == 403, (grants, path)
            assert (
                other.post(url + "/stages", json={"original_upload_name": "x.pdf", "size": 10}).status_code
                == 403
            )
            assert accept(other, url, 2, []).status_code == 403
        user.permissions = ["arm2_cp_view", "arm2_document_view"]
        db.commit()
        assert other.get(url + "/files").status_code == 200
        assert other.get(url + f"/revisions/{revision['id']}/files/{file_id}/download").content == pdf()
        assert other.get(url + "/stages/" + still_pending["stage_id"] + "/preview").status_code == 403
        assert (
            other.post(url + "/stages", json={"original_upload_name": "x.pdf", "size": 10}).status_code == 403
        )
        user.permissions = ["arm2_cp_view", "arm2_cp_edit", "arm2_document_view"]
        db.commit()
        assert (
            other.post(url + "/stages", json={"original_upload_name": "x.pdf", "size": 10}).status_code == 403
        )
        user.permissions = ["arm2_cp_view", "arm2_document_view", "arm2_document_edit"]
        db.commit()
        assert (
            other.post(url + "/stages", json={"original_upload_name": "x.pdf", "size": 10}).status_code == 403
        )
    wrong = create(qa)
    assert c.get(wrong + f"/revisions/{revision['id']}/files/{file_id}/download").status_code == 404


def test_permission_revoked_during_upload_cannot_publish(qa):
    c, db, _, _, _, fake = qa
    url = create(qa)
    other, user = user_client(qa)
    candidate = stage(other, url, pdf("revoked"))
    upload = fake.upload

    def revoke(*args):
        upload(*args)
        user.permissions = ["arm2_cp_view", "arm2_document_view"]
        db.commit()

    fake.upload = revoke
    assert accept(other, url, 1, [candidate]).status_code == 403
    assert bundle(c, url)["revisions"] == []
    assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 0
    other.close()


def test_two_authentic_principals_cannot_overwrite_concurrent_revision(qa):
    c, db, _, _, _, fake = qa
    url = create(qa)
    other, user = user_client(qa)
    first = stage(c, url, pdf("one"))
    second = stage(other, url, pdf("two"))
    barrier = Barrier(2)
    upload = fake.upload

    def synchronized(*args):
        upload(*args)
        barrier.wait(timeout=15)

    fake.upload = synchronized
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: accept(*args).status_code, [(c, url, 1, [first]), (other, url, 1, [second])]
            )
        )
    assert sorted(results) == [200, 409]
    assert len(bundle(c, url)["revisions"]) == 1
    assert db.scalar(select(func.count()).select_from(DocumentFile)) == 1
    assert (
        db.scalar(
            select(func.count()).select_from(HistoryEvent).where(HistoryEvent.object_type == "DOCUMENT")
        )
        == 2
    )
    other.close()


def test_expired_staging_cleanup_only_opaque_local_paths_and_never_accepted_bytes(qa):
    c, db, _, _, _, fake = qa
    url = create(qa)
    candidate = stage(c, url, pdf())
    assert accept(c, url, 1, [candidate]).status_code == 200
    row = db.get(FileStage, candidate["stage_id"])
    row.expires_at = instant() - timedelta(seconds=1)
    db.commit()
    assert storage.private_path(row.id).exists()
    with protected_background_work(db.get_bind()):
        cleanup_expired_staging(db)
        db.commit()
        assert storage.private_path(row.id).exists()
    cleanup_expired_staging(db)
    db.commit()
    assert not storage.private_path(row.id).exists()
    assert len(list(fake.path.iterdir())) == 1
    assert bundle(c, url)["display_state"] == "Доступен"
    expired = stage(c, url, pdf("expiry"))
    row = db.get(FileStage, expired["stage_id"])
    row.expires_at = instant() - timedelta(seconds=1)
    db.commit()
    assert c.get(url + "/stages/" + row.id + "/preview").status_code == 422
    assert accept(c, url, 2, [expired]).status_code == 422


@pytest.mark.parametrize("preview_before_upload", [False, True])
def test_expired_stream_keeps_document_stage_mark_and_bytes_until_lease_is_released(qa, monkeypatch, preview_before_upload):
    c, db, _, _, _, _ = qa
    run_worker = background_operations.work_once
    monkeypatch.setattr(background_operations, "work_once", lambda *_: False)
    url = create(qa)
    identifier = url.split("/")[-1]
    key = ("DOCUMENT", identifier)
    marked = mark(c, *key)
    checked_before = preview(c, key) if preview_before_upload else None
    assert checked_before is None or checked_before["can_execute"]
    original = pdf("stream crossing its expiry")
    stage_id = ok(c.post(url + "/stages", json={"original_upload_name": "stream.pdf", "size": len(original)}), 201)["id"]
    runtime = storage.private_path(stage_id)
    entered, release = Event(), Event()

    async def chunks():
        yield original[:12]
        entered.set()
        assert await asyncio.to_thread(release.wait, 20), "Streaming barrier did not release"
        yield original[12:]

    async def put():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=str(c.base_url),
                                     cookies=dict(c.cookies), headers={"X-CSRF-Token": c.cookies["csrf"]}) as client:
            return await client.put(url + f"/stages/{stage_id}/content", content=chunks())

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(asyncio.run, put())
        try:
            assert entered.wait(15) and runtime.is_file()
            with Session(db.get_bind()) as other:
                assert try_idle_attempt(other, stage_attempt_key(stage_id)) is False
                stage_row = other.get(FileStage, stage_id)
                assert stage_row.state == "empty"
                stage_row.expires_at = instant() - timedelta(seconds=1)
                other.commit()
                cleanup_expired_staging(other)
                other.commit()
            assert runtime.is_file()
            response = c.post(BASE + "/preview", json={"objects": [{"object_type": key[0], "object_id": key[1]}]})
            assert response.status_code == 409 and "ещё выполняется" in response.json()["detail"]
            if checked_before:
                response = execute(c, checked_before["id"])
                assert response.status_code == 409 and "ещё выполняется" in response.json()["detail"]
            assert c.get(url).status_code == 200
            assert db.get(FileStage, stage_id, populate_existing=True).state == "empty"
            assert db.get(DeletionMark, key).command_id == marked["mark"]
            assert db.scalar(select(func.count()).select_from(DeletionBatch).where(DeletionBatch.executed_at.is_not(None))) == 0
            assert db.scalar(select(func.count()).select_from(HistoryEvent).where(HistoryEvent.operation == "DELETED")) == 0
            assert not queue_snapshot(db)
            db.rollback()
        finally:
            release.set()
        assert ok(request.result(timeout=15))["state"] == "ready"
    assert runtime.read_bytes() == original and c.get(url).status_code == 200
    assert c.get(url + f"/stages/{stage_id}").status_code == 422
    checked_after = preview(c, key)
    assert checked_after["can_execute"]
    assert ok(execute(c, checked_after["id"]))["state"] == "FILES_PENDING"
    assert c.get(url).status_code == 404
    assert db.get(FileStage, stage_id, populate_existing=True) is None
    job = db.scalars(select(BackgroundOperation)).one()
    assert job.payload["token"] == stage_id and job.state == "QUEUED"
    db.rollback()
    assert run_worker(db.get_bind(), {CLEANUP: clean_job}) and not runtime.exists()
    assert not run_worker(db.get_bind(), {CLEANUP: clean_job})
    assert ok(c.get(f"{BASE}/batches/{checked_after['id']}"))["state"] == "COMPLETED"


def test_expired_approval_stage_respects_the_shared_cleanup_lease(qa, monkeypatch):
    c, db, _, _, _, _ = qa
    _, issued = letter(c, prepare(qa)[:1])
    request = ok(issued, 201)["request_id"]
    identifier = str(uuid4())
    original = pdf("response-stage")
    ok(c.put(f"/api/arm2/approvals/{request}/response-stages/{identifier}",
             params={"name": "reply.pdf"}, content=original))
    row = db.get(ApprovalAttempt, identifier)
    path = storage.private_path(row.plan["file"]["token"])
    # Advance the expiry check for this disposable stage without changing the immutable row.
    advanced = instant() + timedelta(seconds=storage.STAGE_SECONDS + 1)
    db.rollback()
    monkeypatch.setattr("app.arm2.document_file_store.instant", lambda: advanced)
    with attempt_lease(db, approval_attempt_key(identifier)):
        cleanup_expired_staging(db)
        db.commit()
        assert path.read_bytes() == original
    assert db.get(ApprovalAttempt, identifier) is not None
    cleanup_expired_staging(db)
    db.commit()
    assert not path.exists() and db.get(ApprovalAttempt, identifier) is not None


def test_root_overlap_and_download_adapter_errors_are_sanitized(qa, monkeypatch, tmp_path):
    from app.integrations import DownloadRejected, YandexDisk

    c, *_ = qa
    monkeypatch.setenv("ARM2_DOCUMENT_DISK_ROOT", storage.settings().yandex_disk_root + "/documents")
    with pytest.raises(storage.StorageUnavailable):
        storage.document_root()
    disk = YandexDisk()

    def failed(_):
        raise RuntimeError("OAuth private-secret https://private.invalid/signed-url")

    monkeypatch.setattr(disk, "document_metadata", failed)
    with pytest.raises(DownloadRejected, match="^DOCUMENT_ORIGINAL_UNAVAILABLE$") as error:
        disk.download_document("disk:/private/original", tmp_path / "temporary", "a" * 64, 100)
    assert "secret" not in str(error.value) and "https:" not in str(error.value)


def test_changed_or_overlapping_root_never_reads_or_reuploads_old_originals(qa, monkeypatch):
    c, _, _, _, _, fake = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("immutable root"))]).status_code == 200
    revision = bundle(c, url)["revisions"][0]
    candidate = stage(c, url, pdf("immutable root"))
    monkeypatch.setenv("ARM2_DOCUMENT_DISK_ROOT", "ARM2_TEST/OTHER")
    assert bundle(c, url)["display_state"] == "Файл недоступен"
    assert (
        c.get(url + f"/revisions/{revision['id']}/files/{revision['files'][0]['id']}/download").status_code
        == 503
    )
    assert accept(c, url, 2, [candidate]).status_code == 503
    assert len(fake.uploads) == 1


@pytest.mark.parametrize("corrupt", [False, True])
def test_existing_yandex_adapter_download_checks_bytes_and_keeps_oauth_on_api(monkeypatch, tmp_path, corrupt):
    import hashlib
    import socket

    import httpx

    from app import integrations

    data = pdf("adapter-byte-proof")
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr("app.disk_accounts.configured_token", lambda: "qa-secret-never-log")
    observed = []

    def respond(request):
        observed.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "cloud-api.yandex.net":
            assert request.headers["authorization"] == "OAuth qa-secret-never-log"
            if request.url.path.endswith("/download"):
                return httpx.Response(200, json={"href": "https://storage.yandex.net/private/signed"})
            return httpx.Response(200, json={"type": "file", "sha256": digest, "size": len(data)})
        assert request.headers.get("authorization") is None
        return httpx.Response(200, content=b"corrupt" if corrupt else data)

    client = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        integrations.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    target = tmp_path / "private-output"
    disk = integrations.YandexDisk()
    if corrupt:
        with pytest.raises(integrations.DownloadRejected, match="^DOCUMENT_ORIGINAL_UNAVAILABLE$"):
            disk.download_document("disk:/private/id", target, digest, len(data))
        assert not target.exists()
    else:
        disk.download_document("disk:/private/id", target, digest, len(data))
        assert target.read_bytes() == data
    assert observed[-1] == ("storage.yandex.net", None)
    assert len(observed) == 3  # A corrupt original is rejected after a single read.


@pytest.mark.parametrize("failures", [1, 3])
def test_yandex_original_retries_transient_metadata_without_skipping_integrity(monkeypatch, tmp_path, failures):
    import hashlib

    import httpx

    from app import integrations

    disk = integrations.YandexDisk()
    source = pdf("bounded-retry")
    digest = hashlib.sha256(source).hexdigest()
    attempts = []
    transfers = []

    def metadata(_resource, *, timeout=1.5):
        attempts.append(timeout)
        if len(attempts) <= failures:
            raise httpx.ReadTimeout("private URL or credentials")
        return {"type": "file", "sha256": digest, "size": len(source)}

    request = httpx.Request("GET", "https://cloud-api.yandex.net/v1/disk/resources/download")
    response = httpx.Response(200, request=request, json={"href": "https://storage.yandex.net/signed"})

    def download(_href, path, _hosts):
        transfers.append(path)
        path.write_bytes(source)

    monkeypatch.setattr(disk, "document_metadata", metadata)
    monkeypatch.setattr(disk, "request", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(integrations, "safe_url", lambda url, _hosts: url)
    monkeypatch.setattr(integrations, "download_file", download)
    monkeypatch.setattr(integrations.time, "sleep", lambda _delay: None)
    target = tmp_path / "original"
    if failures == 1:
        disk.download_document("disk:/private/original", target, digest, len(source))
        assert attempts == [1.5, 4] and transfers == [target]
        assert target.read_bytes() == source
    else:
        with pytest.raises(integrations.DownloadRejected, match="^DOCUMENT_ORIGINAL_UNAVAILABLE$"):
            disk.download_document("disk:/private/original", target, digest, len(source))
        assert attempts == [1.5, 4, 4] and not transfers
        assert not target.exists()
