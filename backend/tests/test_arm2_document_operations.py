import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa

from app.arm2.document_file_models import DocumentFile, DocumentFileCorrection, DocumentRevision
from app.arm2.document_models import Document
from app.arm2.history_models import HistoryEvent


def operation(c, url, action, version, **fields):
    return c.post(url + "/operations/" + action, json={"expected_version": version, **fields})


def correct(c, url, version, file_id, stage_id=None, key=None):
    return c.post(
        url + "/corrections",
        json={
            "expected_version": version,
            "idempotency_key": key or str(uuid.uuid4()),
            "file_id": file_id,
            "stage_id": stage_id,
        },
    )


def kind(c, *, parents=None, maximum=None, preset="PARENT_AND_KIND"):
    r = c.post(
        "/api/arm2/document-kinds",
        json={
            "name": "Другой вид " + uuid.uuid4().hex,
            "applicable_object_types": parents or ["CP", "ORG", "VEH"],
            "max_current_documents": maximum,
            "file_naming_template": preset,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def parent(c, token):
    route = {"cp": "counterparties", "org": "organizations", "veh": "vehicles"}[token]
    body = {"name": "Получатель " + uuid.uuid4().hex}
    if token == "cp":
        body["type"] = "Юридическое лицо"
    if token == "veh":
        body["plate_number"] = "B123BC86"
    r = c.post("/api/arm2/" + route, json=body)
    assert r.status_code == 201
    return r.json()["id"]


@pytest.mark.parametrize("token", ["cp", "org", "veh"])
def test_document_lifecycle_retype_move_correction_and_reopen_preserve_originals(qa, token):
    c, db, user, parents, old_kind, disk = qa
    url = create(qa, token)
    docid = url.split("/")[-1]
    data = [pdf("first"), pdf("incorrect"), pdf("third")]
    assert accept(c, url, 1, [stage(c, url, item) for item in data]).status_code == 200
    first = bundle(c, url)["revisions"][0]
    original_refs = {row.id: row.storage_ref for row in db.scalars(select(DocumentFile))}
    old_uploads = len(disk.uploads)
    assert operation(c, url, "archive", 2).json()["display_state"] == "Архивный"
    assert c.get(url.rsplit("/", 1)[0]).json() == []
    assert [r["id"] for r in c.get(url.rsplit("/", 1)[0] + "?archive=archived").json()] == [docid]
    assert len(c.get(url.rsplit("/", 1)[0] + "?archive=all").json()) == 1
    assert operation(c, url, "archive", 3).json()["version"] == 3
    assert operation(c, url, "restore", 3).json()["version"] == 4
    new_kind = kind(c)
    assert operation(c, url, "retype", 4, kind_ref=new_kind).json()["version"] == 5
    assert operation(c, url, "retype", 5, kind_ref=new_kind).json()["version"] == 5
    def check_history_and_current_names(current_url, current_parent_name):
        revisions = bundle(c, current_url)['revisions']
        # All pre-D historical fields must remain byte-for-byte equal. Only the
        # newly exposed computed download_name follows current RETYPE/MOVE data.
        def historical(items):
            return [{**r, 'files':[{k:v for k,v in f.items() if k != 'download_name'} for f in r['files']]}
                    for r in items]
        assert historical(revisions) == historical([first])
        kind_name = c.get('/api/arm2/document-kinds/' + new_kind).json()['name']
        for row, original_bytes in zip(revisions[0]['files'], data, strict=True):
            expected = f"{current_parent_name} {kind_name}-{row['id']}.pdf"
            assert row['download_name'] == expected
            downloaded = c.get(current_url + f"/revisions/{first['id']}/files/{row['id']}/download")
            assert downloaded.status_code == 200 and downloaded.content == original_bytes
            assert unquote(downloaded.headers['content-disposition'].split("filename*=utf-8''")[-1]) == expected
    check_history_and_current_names(url, token.upper())
    target = parent(c, token)
    moved = operation(c, url, "move", 5, target_type=token.upper(), target_id=target)
    assert moved.status_code == 200 and moved.json()["id"] == docid and moved.json()["version"] == 6
    newurl = f"/api/arm2/{token}/{target}/documents/{docid}"
    assert c.get(url).status_code == 404 and c.get(url + "/files").status_code == 404
    assert (
        c.get(url + f"/revisions/{first['id']}/files/{first['files'][0]['id']}/download").status_code == 404
    )
    route = {'cp':'counterparties', 'org':'organizations', 'veh':'vehicles'}[token]
    check_history_and_current_names(newurl, c.get('/api/arm2/' + route + '/' + target).json()['name'])
    for pid in (parents[token], target):
        events = c.get(f"/api/arm2/history/{token.upper()}/{pid}/document-moves")
        assert events.status_code == 200 and len(events.json()) == 1
        e = events.json()[0]
        assert e["object_type"] == "DOCUMENT" and e["object_id"] == docid and e["actor_id"] == str(user.id)
        assert e["changes"]["parent_id"] == {"before": parents[token], "after": target}
    assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 1
    assert len(disk.uploads) == old_uploads
    replacing = stage(c, newurl, pdf("correct replacement"), "correct.pdf")["stage_id"]
    wrong = first["files"][1]["id"]
    key = str(uuid.uuid4())
    result = correct(c, newurl, 6, wrong, replacing, key)
    assert result.status_code == 200
    assert correct(c, newurl, 6, wrong, replacing, key).json() == result.json()
    now = bundle(c, newurl)
    second = now["revisions"][0]
    assert second["source"] == "arm2-api-correct" and second["parent_id"] == target
    assert [second["files"][i]["id"] for i in (0, 2)] == [first["files"][i]["id"] for i in (0, 2)]
    assert len(disk.uploads) == old_uploads + 1
    assert now["revisions"][1]["files"][1]["correction"]["operation"] == "CORRECT"
    for action in ("preview", "download"):
        assert c.get(newurl + f"/revisions/{first['id']}/files/{wrong}/{action}").status_code == 404
    assert disk.location(original_refs[wrong]).read_bytes() == data[1]
    for row in db.scalars(select(DocumentFile)):
        if row.id in original_refs:
            assert row.storage_ref == original_refs[row.id]
    # Identical staged content must not resurrect the tombstoned original under a fresh FILE ID.
    rejected = accept(c, newurl, 7, [stage(c, newurl, data[1])])
    assert rejected.status_code == 422
    assert bundle(c, newurl)["current_revision_id"] == second["id"]
    tombstone = db.get(DocumentFileCorrection, wrong)
    e = db.get(HistoryEvent, tombstone.history_event_id)
    assert e.source == "arm2-api-correct" and e.correlation_id == key and e.object_id == docid
    # Ordinary REVISE remains ordinary and may keep earlier correct originals.
    assert accept(c, newurl, 7, [{"file_id": second["files"][0]["id"]}]).status_code == 200
    last = bundle(c, newurl)["revisions"][0]["files"][0]["id"]
    assert correct(c, newurl, 8, last).status_code == 200
    reopened = bundle(c, newurl)
    assert reopened["display_state"] == "Не загружен" and reopened["revisions"][0]["files"] == []
    assert operation(c, newurl, "archive", 9).status_code == 200
    assert operation(c, newurl, "restore", 10).status_code == 200
    assert c.get(newurl + f"/revisions/{first['id']}/files/{last}/download").status_code == 404
    assert c.get(newurl.rsplit("/", 1)[0]).json()[0]["id"] == docid
    assert db.scalar(select(func.count()).select_from(Document)) == 1


def test_archive_delta_restore_limits_retype_move_conflicts_and_permissions(qa):
    c, db, user, parents, old_kind, disk = qa
    url = create(qa)
    limited = kind(c, maximum=1)
    assert operation(c, url, "retype", 1, kind_ref=limited).status_code == 200
    assert operation(c, url, "archive", 2).status_code == 200
    base = url.rsplit("/", 1)[0]
    other = c.post(base, json={"kind_ref": limited, "name": "Занято"}).json()["id"]
    before = db.scalar(select(func.count()).select_from(HistoryEvent))
    assert operation(c, url, "restore", 3).status_code == 422
    assert c.get(url).json()["version"] == 3
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before
    # A no-op RETYPE with max=1 never counts the source twice.
    assert operation(c, base + "/" + other, "retype", 1, kind_ref=limited).status_code == 200
    bad = kind(c, parents=["ORG"])
    assert operation(c, url, "retype", 3, kind_ref=bad).status_code == 422
    assert operation(c, url, "move", 3, target_type="ORG", target_id=parents["org"]).status_code == 422
    assert operation(c, url, "restore", 1).status_code == 409
    user.role = "OPERATOR"
    user.permissions = ["arm2_cp_view", "arm2_document_view"]
    db.commit()
    assert operation(c, url, "archive", 3).status_code == 403
    assert c.get(f"/api/arm2/history/CP/{parents['cp']}/document-moves").status_code == 200
    user.permissions = ["arm2_cp_view"]
    db.commit()
    assert c.get(f"/api/arm2/history/CP/{parents['cp']}/document-moves").status_code == 403
    assert correct(c, url, 3, "FILE-000001").status_code == 403


@pytest.mark.parametrize("failure", ["remote", "sql"])
def test_correction_failure_atomicity_same_key_retry_and_no_physical_delete(qa, monkeypatch, failure):
    c, db, user, parents, old_kind, disk = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("wrong"))]).status_code == 200
    before = bundle(c, url)
    wrong = before["revisions"][0]["files"][0]["id"]
    candidate = stage(c, url, pdf("right"))["stage_id"]
    key = str(uuid.uuid4())
    history = db.scalar(select(func.count()).select_from(HistoryEvent))

    def reject(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO arm2_document_file_corrections"):
            raise OperationalError("injected correction SQL failure", {}, RuntimeError("qa"))

    if failure == "remote":
        disk.fail_at = len(disk.uploads)
    else:
        event.listen(db.get_bind(), "before_cursor_execute", reject)
    try:
        assert correct(c, url, 2, wrong, candidate, key).status_code == 503
    finally:
        disk.fail_at = None
        if failure == "sql":
            event.remove(db.get_bind(), "before_cursor_execute", reject)
    db.expire_all()
    assert bundle(c, url) == before
    assert db.scalar(select(func.count()).select_from(DocumentFileCorrection)) == 0
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == history
    assert correct(c, url, 2, wrong, candidate, key).status_code == 200
    uploads = len(disk.uploads)
    assert correct(c, url, 2, wrong, candidate, key).status_code == 200
    assert len(disk.uploads) == uploads
    assert db.scalar(select(func.count()).select_from(DocumentFileCorrection)) == 1


def test_two_authenticated_restore_requests_cannot_both_take_last_slot(qa):
    c, db, user, parents, old_kind, disk = qa
    first = create(qa)
    second = create(qa)
    assert operation(c, first, "archive", 1).status_code == 200
    assert operation(c, second, "archive", 1).status_code == 200
    info = c.get("/api/arm2/document-kinds/" + old_kind).json()
    assert (
        c.put(
            "/api/arm2/document-kinds/" + old_kind,
            json={
                **{k: v for k, v in info.items() if k not in ("id", "version")},
                "expected_version": info["version"],
                "max_current_documents": 1,
            },
        ).status_code
        == 200
    )
    from app.api import app
    from app.auth import hasher
    from app.models import User

    second_user = User(
        login="restore-second",
        name="restore-second",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
        password_hash=hasher.hash("qa-restore-password"),
    )
    db.add(second_user)
    db.commit()
    with TestClient(app) as other:
        assert (
            other.post(
                "/api/auth/login", json={"login": "restore-second", "password": "qa-restore-password"}
            ).status_code
            == 200
        )
        other.headers["X-CSRF-Token"] = other.cookies["csrf"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(operation, client, url, "restore", 2)
                for client, url in ((c, first), (other, second))
            ]
            assert sorted(f.result().status_code for f in futures) == [200, 422]
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(Document).where(Document.archive_state == "Неархивный"))
        == 1
    )


def test_corrected_audit_rows_and_parent_snapshot_are_immutable(qa):
    c, db, *_ = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    first = bundle(c, url)["revisions"][0]
    assert correct(c, url, 2, first["files"][0]["id"]).status_code == 200
    from sqlalchemy.exc import DBAPIError

    for table in ("arm2_document_file_corrections", "arm2_document_revision_contexts"):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.execute(text("DELETE FROM " + table))
    with pytest.raises(DBAPIError, match="Corrected file"), db.begin_nested():
        db.execute(
            text("UPDATE arm2_documents SET current_revision_id=:revision WHERE id=:doc"),
            {"revision": first["id"], "doc": url.split("/")[-1]},
        )
        db.execute(text("SET CONSTRAINTS document_correction_guard IMMEDIATE"))


def test_correction_rechecks_permissions_after_durable_upload_and_rejects_stale_or_identical(qa, monkeypatch):
    c, db, user, parents, old_kind, disk = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("wrong"))]).status_code == 200
    before = bundle(c, url)
    wrong = before["revisions"][0]["files"][0]["id"]
    identical = stage(c, url, pdf("wrong"))["stage_id"]
    assert correct(c, url, 2, wrong, identical).status_code == 422
    candidate = stage(c, url, pdf("right"))["stage_id"]
    assert correct(c, url, 1, wrong, candidate).status_code == 409
    original_upload = disk.upload

    def revoke_after_upload(*args):
        original_upload(*args)
        user.role = "OPERATOR"
        user.permissions = ["arm2_cp_view", "arm2_document_view"]
        db.commit()

    monkeypatch.setattr(disk, "upload", revoke_after_upload)
    assert correct(c, url, 2, wrong, candidate).status_code == 403
    assert bundle(c, url) == before
    assert db.scalar(select(func.count()).select_from(DocumentFileCorrection)) == 0
    assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 1


def test_concurrent_moves_and_opposite_retypes_are_serialized_without_limit_bypass(qa):
    c, db, user, parents, old_kind, disk = qa
    limited = kind(c, maximum=1)
    first = create(qa)
    assert operation(c, first, "retype", 1, kind_ref=limited).status_code == 200
    source_b = parent(c, "cp")
    destination = parent(c, "cp")
    base_b = f"/api/arm2/cp/{source_b}/documents"
    second_id = c.post(base_b, json={"kind_ref": limited, "name": "Второй"}).json()["id"]
    second = base_b + "/" + second_id
    from app.api import app
    from app.auth import hasher
    from app.models import User

    other_user = User(
        login="move-second",
        name="move-second",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
        password_hash=hasher.hash("qa-move-password"),
    )
    db.add(other_user)
    db.commit()
    with TestClient(app) as other:
        assert (
            other.post(
                "/api/auth/login", json={"login": "move-second", "password": "qa-move-password"}
            ).status_code
            == 200
        )
        other.headers["X-CSRF-Token"] = other.cookies["csrf"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(operation, client, url, "move", version, target_type="CP", target_id=destination)
                for client, url, version in ((c, first, 2), (other, second, 1))
            ]
            statuses = sorted(f.result(timeout=15).status_code for f in futures)
            assert statuses == [200, 422]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Document).where(Document.parent_id == destination)) == 1
    # Reciprocal RETYPE operations acquire old/new kind locks in the same order.
    a = kind(c, maximum=1)
    b = kind(c, maximum=1)
    base = first.rsplit("/", 1)[0]
    ids = [c.post(base, json={"kind_ref": k, "name": "Обмен " + k}).json()["id"] for k in (a, b)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(operation, c, base + "/" + doc, "retype", 1, kind_ref=k)
            for doc, k in zip(ids, (b, a), strict=True)
        ]
        assert [f.result(timeout=15).status_code for f in futures] == [422, 422]


def test_retype_validates_existing_naming_contract_before_committing_a_legacy_type(qa):
    from urllib.parse import unquote

    c, db, user, parents, old_kind, disk = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("unchanged original"))]).status_code == 200
    before = bundle(c, url)
    target = kind(c)
    # Represent an untouched 0064 legacy DOC-KIND: B3-C deliberately preserves NULL.
    db.execute(text("UPDATE arm2_document_kinds SET file_naming_template=NULL WHERE id=:id"), {"id": target})
    db.commit()
    history = db.scalar(select(func.count()).select_from(HistoryEvent))
    uploads = len(disk.uploads)
    assert operation(c, url, "retype", 2, kind_ref=target).status_code == 422
    assert bundle(c, url) == before
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == history
    info = c.get("/api/arm2/document-kinds/" + target).json()
    body = {k: v for k, v in info.items() if k not in ("id", "version")}
    body.update(expected_version=info["version"], file_naming_template="PARENT_AND_KIND")
    assert c.put("/api/arm2/document-kinds/" + target, json=body).status_code == 200
    assert operation(c, url, "retype", 2, kind_ref=target).status_code == 200
    revision = before["revisions"][0]
    file = revision["files"][0]
    response = c.get(url + f"/revisions/{revision['id']}/files/{file['id']}/download")
    assert response.status_code == 200 and response.content == pdf("unchanged original")
    assert info["name"] in unquote(response.headers["content-disposition"])
    assert bundle(c, url)["revisions"] == before["revisions"]
    assert len(disk.uploads) == uploads
