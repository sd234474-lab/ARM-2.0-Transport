"""Use real authenticated ARM2 routes and disposable PostgreSQL/FILE only."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_arm2_calibration_approval import letter, prepare
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import qa as qa

from app.arm2 import background_operations
from app.arm2.background_operations import work_once
from app.arm2.bulk_models import BulkReceipt, BulkSession
from app.arm2.deletion_cleanup import clean_job
from app.arm2.deletion_models import DeletionBatch
from app.arm2.deletion_service import CLEANUP
from app.arm2.document_file_models import DocumentFile
from app.arm2.file_read_guard import READ_KEY
from app.arm2.history_models import HistoryEvent
from app.arm2.operation_models import BackgroundOperation
from app.arm2.vehicle_models import Vehicle
from app.auth import hasher
from app.db import engine
from app.models import User

BASE = "/api/arm2/deletion"


@pytest.fixture(autouse=True)
def control_worker(monkeypatch):
    # Tests advance the real queue explicitly to observe durable intermediate states.
    monkeypatch.setattr(background_operations, "work_once", lambda *_args: False)


def ok(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def mark(client, kind, identifier, value=True):
    route = f"{BASE}/marks/{kind}/{identifier}"
    before = ok(client.get(route))
    return ok(client.put(route, json={
        "expected_version": before["version"], "expected_mark": before["mark"], "marked": value,
    }))


def preview(client, *identities):
    return ok(client.post(f"{BASE}/preview", json={"objects": [
        {"object_type": kind, "object_id": identifier} for kind, identifier in identities
    ]}))


def execute(client, preview_id, confirmed=True):
    return client.post(f"{BASE}/batches/{preview_id}/execute", json={"confirmed": confirmed})


def test_shared_mark_permissions_version_reopen_and_history(qa):
    c, db, user, parents, kind, _ = qa
    url = f"/api/arm2/document-kinds/{kind}"
    initial = ok(c.get(url))
    # Keep existing access but separate the platform capabilities.
    user.role = "OPERATOR"
    user.permissions = ["arm2_doc_kind_view", "arm2_doc_kind_edit"]
    db.commit()
    route = f"{BASE}/marks/DOC-KIND/{kind}"
    assert c.put(route, json={"expected_version": initial["version"], "expected_mark": None, "marked": True}).status_code == 403
    user.permissions = [*user.permissions, "arm2_delete_mark"]
    db.commit()
    marked = mark(c, "DOC-KIND", kind)
    assert marked["mark"] and ok(c.get(route)) == marked
    assert ok(c.get(url)) == initial
    check = preview(c, ("DOC-KIND", kind))
    assert not check["can_execute"]
    assert execute(c, check["id"]).status_code == 403
    assert c.put(route, json={"expected_version": initial["version"], "expected_mark": None, "marked": False}).status_code == 409
    assert mark(c, "DOC-KIND", kind, False)["mark"] is None
    assert ok(c.get(BASE + "/marks"))["items"] == []
    assert [row["operation"] for row in ok(c.get(f"/api/arm2/history/DOC-KIND/{kind}"))][:2] == ["UNMARKED", "MARKED"]


def test_dependency_added_after_preview_denies_whole_batch(qa):
    c, _, _, parents, kind, _ = qa
    mark(c, "DOC-KIND", kind)
    mark(c, "VEH", parents["veh"])
    check = preview(c, ("DOC-KIND", kind), ("VEH", parents["veh"]))
    assert check["can_execute"]
    document = create(qa, "veh").split("/")[-1]
    assert execute(c, check["id"], confirmed=False).status_code == 422
    assert execute(c, check["id"]).status_code == 409
    assert ok(c.get("/api/arm2/document-kinds/" + kind))["id"] == kind
    assert ok(c.get("/api/arm2/vehicles/" + parents["veh"]))["id"] == parents["veh"]
    blocked = preview(c, ("DOC-KIND", kind), ("VEH", parents["veh"]))
    assert not blocked["can_execute"]
    assert any(row["object_id"] == document and row["blocking"] and row["field"] == "kind_ref"
               for row in blocked["dependencies"])


def test_document_revisions_remain_immutable_until_selected_and_cloud_cleanup_is_recoverable(qa, monkeypatch):
    c, db, _, parents, _, provider = qa
    url = create(qa, "veh")
    doc_id = url.split("/")[-1]
    assert accept(c, url, 1, [stage(c, url, pdf("original"))]).status_code == 200
    assert accept(c, url, 2, [stage(c, url, pdf("new edition"))]).status_code == 200
    revisions = bundle(c, url)["revisions"]
    ids = {row["id"] for revision in revisions for row in revision["files"]}
    storage_refs = {record.storage_ref for record in db.scalars(select(DocumentFile))}
    actual_paths = {provider.location(path) for path in storage_refs}
    db.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        with engine().begin() as conn:
            conn.execute(delete(DocumentFile).where(DocumentFile.id.in_(ids)))
    mark(c, "VEH", parents["veh"])
    mark(c, "DOCUMENT", doc_id)
    check = preview(c, ("VEH", parents["veh"]), ("DOCUMENT", doc_id))
    assert check["can_execute"], check
    assert ids <= {d["object_id"] for d in check["dependencies"] if d.get("object_type") == "arm2_document_files"}
    with engine().begin() as opened_request:
        opened_request.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": READ_KEY})
        assert execute(c, check["id"]).status_code == 409
    result = ok(execute(c, check["id"]))
    assert result["state"] == "FILES_PENDING" and all(path.exists() for path in actual_paths)
    assert execute(c, check["id"]).status_code == 409
    assert c.get(url).status_code == 404
    assert c.get(f"{url}/revisions/{revisions[0]['id']}/files/{revisions[0]['files'][0]['id']}/download").status_code == 404
    assert not db.scalars(select(DocumentFile)).all()
    assert not db.scalars(select(Vehicle).where(Vehicle.id == parents["veh"])).all()
    audit = db.get(DeletionBatch, check["id"])
    assert set(audit.artifacts["arm2_document_files"]) == ids
    assert set(audit.artifacts["arm2_document_revisions"]) == {row["id"] for row in revisions}
    db.rollback()
    event = ok(c.get(f"/api/arm2/history/DOCUMENT/{doc_id}"))[0]
    assert event["operation"] == "DELETED"
    assert ok(c.get(f"/api/arm2/history/events/{event['event_id']}"))["correlation_id"] == check["id"]

    def trash(path, _digest):
        provider.location(path).unlink(missing_ok=True)

    with monkeypatch.context() as context:
        def fail(_path, _digest):
            raise TimeoutError("synthetic provider timeout")
        context.setattr(provider, "trash_exchange_file", fail, raising=False)
        while work_once(engine(), {CLEANUP: clean_job}):
            pass
    assert ok(c.get(f"{BASE}/batches/{check['id']}"))["state"] == "FILES_PENDING"
    assert all(path.exists() for path in actual_paths)
    with engine().begin() as conn:
        conn.execute(update(BackgroundOperation).where(BackgroundOperation.kind == CLEANUP).values(
            not_before=datetime.now(UTC) - timedelta(seconds=1)))
    monkeypatch.setattr(provider, "trash_exchange_file", trash, raising=False)
    altered = next(iter(actual_paths))
    original = altered.read_bytes()
    altered.write_bytes(b"Unexpected bytes")
    while work_once(engine(), {CLEANUP: clean_job}):
        pass
    assert ok(c.get(f"{BASE}/batches/{check['id']}"))["state"] == "FILES_BLOCKED"
    assert altered.read_bytes() == b"Unexpected bytes"
    altered.write_bytes(original)
    assert ok(c.post(f"{BASE}/batches/{check['id']}/retry"))["state"] == "FILES_PENDING"
    while work_once(engine(), {CLEANUP: clean_job}):
        pass
    receipt = ok(c.get(f"{BASE}/batches/{check['id']}"))
    assert receipt["state"] == "COMPLETED"
    assert all(not path.exists() for path in actual_paths)
    assert ok(c.get(BASE + "/batches"))["items"] == [receipt]


@pytest.mark.parametrize(("kind", "collection"), [("CP", "counterparties"), ("ORG", "organizations")])
def test_party_and_signer_are_selected_explicitly(qa, kind, collection):
    c, _, _, parents, _, _ = qa
    identifier = parents[kind.lower()]
    url = f"/api/arm2/{collection}/{identifier}"
    rep = ok(c.post(url + "/representatives", json={"name": "Synthetic signer", "state": "Архивный"}), 201)
    rep_type = kind + "REP"
    party = ok(c.get(url))
    ok(c.put(url, json={"expected_version": party["version"], "name": party["name"], "primary_signer_id": rep["id"]}))
    mark(c, kind, identifier)
    mark(c, rep_type, rep["id"])
    assert ok(c.get(url + "/representatives/" + rep["id"]))["state"] == "Архивный"
    assert not preview(c, (kind, identifier))["can_execute"]
    assert not preview(c, (rep_type, rep["id"]))["can_execute"]
    check = preview(c, (kind, identifier), (rep_type, rep["id"]))
    assert check["can_execute"]
    assert ok(execute(c, check["id"]))["state"] == "COMPLETED"
    assert c.get(url).status_code == 404
    assert ok(c.get(f"/api/arm2/history/{rep_type}/{rep['id']}"))[0]["operation"] == "DELETED"


def test_type_dependency_approval_evidence_and_customer_files(qa):
    c, db, _, _, _, fake = qa
    vehicles = prepare(qa)
    _, answer = letter(c, vehicles)
    approval_id = ok(answer, 201)["request_id"]
    approval_path = "/api/arm2/approvals/" + approval_id
    agreement = ok(c.get(approval_path))
    vehicle_id, document_path, _, _ = vehicles[0]
    document_id = document_path.split("/")[-1]
    vehicle = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    type_id = vehicle["type_id"]
    mark(c, "TYPE", type_id)
    assert any(link["object_id"] == vehicle_id and link["blocking"] for link in
               preview(c, ("TYPE", type_id))["dependencies"])
    mark(c, "VEH", vehicle_id)
    mark(c, "DOCUMENT", document_id)
    blocked = preview(c, ("VEH", vehicle_id), ("DOCUMENT", document_id))
    assert not blocked["can_execute"]
    assert any(link["object_id"] == approval_id and link["field"] == "vehicles_snapshot"
               and link["blocking"] for link in blocked["dependencies"])
    mark(c, "APPROVAL", approval_id)
    assert not preview(c, ("APPROVAL", approval_id))["can_execute"]
    decisions = [row["id"] for row in agreement["decisions"]]
    for identifier in decisions:
        mark(c, "APPROVAL-ITEM", identifier)
    check = preview(c, ("APPROVAL", approval_id), *(("APPROVAL-ITEM", key) for key in decisions))
    assert check["can_execute"], check
    result = ok(execute(c, check["id"]))
    assert result["state"] == "FILES_PENDING" and len(result["cleanup"]) == 2
    assert c.get(approval_path).status_code == 404
    assert c.get(document_path).status_code == 200
    assert ok(c.get("/api/arm2/vehicles/" + vehicle_id))["calibration_m3"] == 25
    assert ok(c.get(f"/api/arm2/history/APPROVAL-ITEM/{decisions[0]}"))[0]["operation"] == "DELETED"
    assert all(fake.location(ref).exists() for ref in fake.uploads)
    assert db.scalar(select(DocumentFile.id).where(DocumentFile.process_id == approval_id)) is None


def test_logical_references_and_existing_receipts_remain_visible(qa):
    c, db, user, parents, _, _ = qa
    identifier = parents["veh"]
    mark(c, "VEH", identifier)
    draft = BulkSession(id=str(uuid4()), user_id=user.id, source="Synthetic",
                        payload={"plans": [{"target": identifier}]})
    db.add(draft)
    db.commit()
    check = preview(c, ("VEH", identifier))
    assert not check["can_execute"]
    assert any(row["object_id"] == draft.id and row["field"] == "payload" for row in check["dependencies"])
    db.add(BulkReceipt(
        command_id=str(uuid4()), session_id=draft.id, user_id=user.id, source="Synthetic",
        fingerprint="f" * 64, row_key="row-1", vehicle_id=identifier, version_after=1, fields=[],
    ))
    db.delete(draft)
    db.commit()
    check = preview(c, ("VEH", identifier))
    assert not check["can_execute"]
    assert any("Квитанция импорта" in row["reason"] and row["blocking"] for row in check["dependencies"])


def test_version_change_conflicts_and_id_is_not_reused(qa):
    c, db, _, _, _, _ = qa
    editor = User(login="deletion-test-editor", name="Test editor", active=True, role="OPERATOR",
                  password_hash=hasher.hash("test-editor-password"),
                  permissions=["arm2_type_view", "arm2_type_edit"])
    db.add(editor)
    db.commit()
    other = TestClient(c.app)
    assert other.post("/api/auth/login", json={"login": editor.login, "password": "test-editor-password"}).status_code == 200
    other.headers["X-CSRF-Token"] = other.cookies["csrf"]
    path = "/api/arm2/types"
    created = ok(c.post(path, json={"name": "Synthetic type"}), 201)
    kind, identifier = "TYPE", created["id"]
    mark(c, kind, identifier)
    checked = preview(c, (kind, identifier))
    ok(other.put(path + "/" + identifier, json={
        **{field: created[field] for field in ("requires_sts", "requires_calibration", "requires_wialon")},
        "expected_version": created["version"], "name": "Renamed",
    }))
    assert execute(c, checked["id"]).status_code == 409
    checked = preview(c, (kind, identifier))
    assert ok(execute(c, checked["id"]))["state"] == "COMPLETED"
    assert c.get(path + "/" + identifier).status_code == 404
    again = ok(c.post(path, json={"name": "Synthetic type"}), 201)
    assert again["id"] != identifier


def test_execution_sql_failure_leaves_no_partial_domain_changes_or_cleanup(qa):
    c, db, _, parents, _, provider = qa
    url = create(qa, "veh")
    doc_id = url.split("/")[-1]
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    mark(c, "DOCUMENT", doc_id)
    mark(c, "VEH", parents["veh"])
    checked = preview(c, ("DOCUMENT", doc_id), ("VEH", parents["veh"]))
    assert checked["can_execute"]

    def reject_vehicle_delete(_connection, _cursor, statement, *_args):
        if statement.startswith("DELETE FROM arm2_vehicles"):
            raise IntegrityError(statement, {}, RuntimeError("Synthetic SQL rejection"))

    event.listen(engine(), "before_cursor_execute", reject_vehicle_delete)
    try:
        assert execute(c, checked["id"]).status_code == 409
    finally:
        event.remove(engine(), "before_cursor_execute", reject_vehicle_delete)
    assert c.get(url).status_code == 200
    assert len(bundle(c, url)["revisions"]) == 1
    assert db.get(DeletionBatch, checked["id"]).executed_at is None
    assert db.scalar(select(BackgroundOperation.id).where(BackgroundOperation.kind == CLEANUP)) is None
    assert all(provider.location(ref).exists() for ref in provider.uploads)
    db.rollback()
    assert ok(execute(c, checked["id"]))["state"] == "FILES_PENDING"


def test_migration_preserves_existing_history_and_rejects_loss_of_new_audit(qa):
    from alembic.config import Config

    from alembic import command

    c, db, _, _, kind, _ = qa
    statement = select(HistoryEvent.event_id, HistoryEvent.object_id, HistoryEvent.changes).order_by(HistoryEvent.event_id)
    old_history = db.execute(statement).all()
    db.rollback()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "0074")
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0074"
    db.rollback()
    command.upgrade(config, "head")
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0075"
    assert db.execute(statement).all() == old_history
    db.rollback()
    mark(c, "DOC-KIND", kind)
    with pytest.raises(RuntimeError, match="deletion audit exists"):
        command.downgrade(config, "0074")
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0075"


def test_unknown_incoming_fk_fails_closed(qa):
    c, _, _, _, kind, _ = qa
    mark(c, "DOC-KIND", kind)
    with engine().begin() as conn:
        conn.exec_driver_sql("CREATE TABLE synthetic_link (id varchar REFERENCES arm2_document_kinds(id))")
    try:
        response = c.post(BASE + "/preview", json={"objects": [
            {"object_type": "DOC-KIND", "object_id": kind},
        ]})
        assert response.status_code == 409, response.text
        assert "synthetic_link" in response.json()["detail"]
        assert c.get("/api/arm2/document-kinds/" + kind).status_code == 200
    finally:
        with engine().begin() as conn:
            conn.exec_driver_sql("DROP TABLE synthetic_link")
