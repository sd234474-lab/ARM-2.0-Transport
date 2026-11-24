"""Use real authenticated ARM2 routes and disposable PostgreSQL/FILE only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, delete, event, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from test_arm2_calibration_approval import letter, prepare
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import qa as qa
from test_arm2_questionnaire_workflow import b5 as b5

from app.arm2 import background_operations
from app.arm2.background_operations import work_once
from app.arm2.bulk_models import BulkReceipt, BulkSession
from app.arm2.deletion_cleanup import clean_job
from app.arm2.deletion_models import DeletionBatch, DeletionMark
from app.arm2.deletion_service import CLEANUP
from app.arm2.document_file_models import DocumentFile
from app.arm2.document_models import Document
from app.arm2.file_read_guard import READ_KEY
from app.arm2.history_models import HistoryEvent
from app.arm2.object_registry import OBJECT_REGISTRY, DeletionType, ObjectDefinition, ObjectRegistryError
from app.arm2.operation_models import BackgroundOperation
from app.arm2.questionnaire_models import QuestionnaireExport
from app.arm2.vehicle_models import Vehicle
from app.auth import hasher
from app.db import engine
from app.models import User
from app.permissions import Permission

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
    db.rollback()
    with engine().begin() as conn:
        conn.execute(update(BackgroundOperation).where(
            BackgroundOperation.payload["batch_id"].astext == check["id"]
        ).values(state="FAILED", failure={"code": "REVIEW"}))
    prior = queue_snapshot(db)
    user = db.get(User, result["actor_id"])
    user.role = "OPERATOR"
    user.permissions = ["arm2_delete_mark", "arm2_delete_execute", "arm2_veh_view", "arm2_veh_edit", "arm2_document_view"]
    db.commit()
    retry_url = f"{BASE}/batches/{check['id']}/retry"
    assert c.post(retry_url).status_code == 403
    assert queue_snapshot(db) == prior
    user.permissions = [*user.permissions, "arm2_document_edit"]
    db.commit()
    assert ok(c.post(retry_url))["state"] == "FILES_PENDING"
    assert all(fake.location(ref).exists() for ref in fake.uploads)


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


def test_questionnaire_export_keeps_its_counterparty_and_receipt(b5):
    c, db, _, parents, _, _ = b5
    cp_id = parents["cp"]
    exported = ok(c.post(f"/api/arm2/questionnaires/exports/{cp_id}", json={"format": "xlsx"}))
    assert exported["data"] and exported["filename"].endswith(".xlsx")
    export_id = db.scalar(select(QuestionnaireExport.id).where(QuestionnaireExport.cp_id == cp_id))
    assert export_id is not None
    db.rollback()
    mark(c, "CP", cp_id)
    checked = preview(c, ("CP", cp_id))
    assert not checked["can_execute"]
    assert any(item["object_id"] == export_id and item["field"] == "cp_id" and item["blocking"]
               for item in checked["dependencies"])
    assert execute(c, checked["id"]).status_code == 409
    assert ok(c.get(f"/api/arm2/counterparties/{cp_id}"))["id"] == cp_id
    assert db.get(QuestionnaireExport, export_id).cp_id == cp_id
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None
    assert not queue_snapshot(db)


def test_worker_resumes_after_interruption_between_trash_and_final_receipt(qa, monkeypatch):
    c, db, _, _, _, provider = qa
    url = create(qa, "veh")
    document_id = url.split("/")[-1]
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    mark(c, "DOCUMENT", document_id)
    checked = preview(c, ("DOCUMENT", document_id))
    assert ok(execute(c, checked["id"]))["state"] == "FILES_PENDING"
    calls = []

    def interrupted_after_trash(ref, _digest):
        calls.append(ref)
        provider.location(ref).unlink()
        raise SystemExit("Synthetic process interruption after provider commit")

    monkeypatch.setattr(provider, "trash_exchange_file", interrupted_after_trash, raising=False)
    with pytest.raises(SystemExit, match="Synthetic process interruption"):
        while work_once(engine(), {CLEANUP: clean_job}):
            pass
    assert calls == provider.uploads
    assert not provider.location(calls[0]).exists()
    running = [row for row in queue_snapshot(db).values() if row["state"] == "RUNNING"]
    assert len(running) == 1 and running[0]["payload"]["storage_ref"] == calls[0]
    assert ok(c.get(f"{BASE}/batches/{checked['id']}"))["state"] == "FILES_PENDING"
    while work_once(engine(), {CLEANUP: clean_job}):
        pass
    assert calls == provider.uploads
    assert ok(c.get(f"{BASE}/batches/{checked['id']}"))["state"] == "COMPLETED"


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


class SyntheticBase(DeclarativeBase):
    pass


class SyntheticDeletionObject(SyntheticBase):
    __tablename__ = "synthetic_deletion_object"
    id: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int]


def test_new_type_registration_requires_and_uses_removal_policy(qa, monkeypatch):
    c, db, _, _, _, _ = qa
    with engine().begin() as conn:
        SyntheticBase.metadata.create_all(conn)
        conn.execute(SyntheticDeletionObject.__table__.insert(), {"id": "SAMPLE-000001", "version": 1})
        # Extend only the isolated test schema so HIST accepts the synthetic type.
        for constraint in HistoryEvent.__table__.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name in {
                "ck_arm2_history_object_type", "ck_arm2_history_object_id",
            }:
                conn.exec_driver_sql(f"ALTER TABLE arm2_history_events DROP CONSTRAINT {constraint.name}")
                conn.exec_driver_sql(f"ALTER TABLE arm2_history_events ADD CONSTRAINT {constraint.name} "
                    f"CHECK (({constraint.sqltext}) OR (object_type='SAMPLE' AND object_id='SAMPLE-000001'))")

    known = OBJECT_REGISTRY.deletion_definitions()
    monkeypatch.setattr(OBJECT_REGISTRY, "_definitions", OBJECT_REGISTRY._definitions.copy())
    monkeypatch.setattr(OBJECT_REGISTRY, "_deletion", {})
    OBJECT_REGISTRY.register(ObjectDefinition("SAMPLE", "SAMPLE", {}, {}, edit_permission=Permission.ARM2_TYPE_EDIT))
    with pytest.raises(ObjectRegistryError, match="Не для всех типов"):
        OBJECT_REGISTRY.register_deletion(known)
    with pytest.raises(ObjectRegistryError, match="Не определена политика"):
        OBJECT_REGISTRY.register_deletion({**known, "SAMPLE": DeletionType(
            SyntheticDeletionObject, 0, collection="/arm2/sample")})

    definitions = {**known, "SAMPLE": DeletionType(SyntheticDeletionObject, 180, collection="/arm2/sample")}
    OBJECT_REGISTRY.register_deletion(definitions)
    key = ("SAMPLE", "SAMPLE-000001")
    selected = mark(c, *key)
    checked = preview(c, key)

    # An incomplete registry must also be rejected at runtime before any mutation.
    with monkeypatch.context() as broken:
        broken.setattr(OBJECT_REGISTRY, "_deletion", known)
        denied = execute(c, checked["id"])
        assert denied.status_code == 422 and "политика удаления" in denied.json()["detail"]
    with monkeypatch.context() as broken:
        broken.setattr(OBJECT_REGISTRY, "_deletion", {**definitions, "TYPE": replace(known["TYPE"], order=1)})
        denied = execute(c, checked["id"])
        assert denied.status_code == 422 and "порядок удаления" in denied.json()["detail"]
    assert db.get(SyntheticDeletionObject, key[1]).version == 1
    assert db.get(DeletionMark, key).command_id == selected["mark"]
    assert db.get(DeletionBatch, checked["id"]).executed_at is None
    assert not db.scalars(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")).all()
    assert not db.scalars(select(BackgroundOperation)).all()
    db.rollback()

    completed = ok(execute(c, checked["id"]))
    assert completed["state"] == "COMPLETED"
    assert c.get(f"{BASE}/marks/{key[0]}/{key[1]}").status_code == 404
    assert db.get(SyntheticDeletionObject, key[1]) is None and db.get(DeletionMark, key) is None
    audit = db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED", HistoryEvent.object_id == key[1]))
    assert audit.object_type == key[0] and audit.correlation_id == checked["id"]


@pytest.mark.parametrize(("trigger", "body", "expected_text"), [
    ("BEFORE", "RETURN NULL;", "База не подтвердила удаление всех записей набора"),
    ("AFTER", "INSERT INTO arm2_vehicles SELECT (OLD).*; RETURN NULL;", "После удаления в базе остались записи набора"),
])
def test_delete_returning_and_absence_roll_back_all_rows(qa, trigger, body, expected_text):
    c, db, _, parents, _, provider = qa
    url = create(qa, "veh")
    document_id = url.split("/")[-1]
    vehicle_id = parents["veh"]
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    prior_bundle = bundle(c, url)
    prior_pointer = db.get(Document, document_id).current_revision_id
    originals = {ref: provider.location(ref).read_bytes() for ref in provider.uploads}
    db.rollback()
    keys = [("VEH", vehicle_id), ("DOCUMENT", document_id)]
    marks = {key: mark(c, *key)["mark"] for key in keys}
    checked = preview(c, *keys)
    assert checked["can_execute"]
    with engine().begin() as conn:
        conn.exec_driver_sql(f"CREATE FUNCTION synthetic_delete_guard() RETURNS trigger LANGUAGE plpgsql "
                             f"AS $$ BEGIN {body} END $$")
        conn.exec_driver_sql(f"CREATE TRIGGER synthetic_delete_guard {trigger} DELETE ON arm2_vehicles "
                             "FOR EACH ROW EXECUTE FUNCTION synthetic_delete_guard()")

    result = execute(c, checked["id"])
    assert result.status_code == 409 and result.json()["detail"] == expected_text
    assert bundle(c, url) == prior_bundle
    assert db.get(Document, document_id).current_revision_id == prior_pointer
    assert db.get(Vehicle, vehicle_id) is not None
    batch = db.get(DeletionBatch, checked["id"])
    assert batch.executed_at is batch.execution_tx is batch.artifacts is None
    assert {key: db.get(DeletionMark, key).command_id for key in keys} == marks
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None
    assert not db.scalars(select(BackgroundOperation)).all()
    assert all(provider.location(ref).read_bytes() == data for ref, data in originals.items())


def queue_snapshot(db):
    query = "SELECT id, row_to_json(j)::jsonb FROM arm2_background_operations j ORDER BY id"
    rows = dict(db.execute(text(query)).all())
    db.rollback()
    return rows


def test_manual_retry_requires_current_permissions_for_every_object_in_batch(qa, monkeypatch):
    c, db, user, _, _, provider = qa
    urls = [create(qa, token) for token in ("cp", "org")]
    for number, url in enumerate(urls):
        assert accept(c, url, 1, [stage(c, url, pdf(str(number)))]).status_code == 200
    documents = [url.split("/")[-1] for url in urls]
    for identifier in documents:
        mark(c, "DOCUMENT", identifier)
    checks = [preview(c, ("DOCUMENT", identifier))["id"] for identifier in documents]
    for batch_id in checks:
        assert ok(execute(c, batch_id))["state"] == "FILES_PENDING"
    with engine().begin() as conn:
        # One blocked original in each actual batch, plus still-queued local work.
        conn.execute(update(BackgroundOperation).where(
            BackgroundOperation.kind == CLEANUP, BackgroundOperation.payload["storage_ref"].astext.is_not(None)
        ).values(state="FAILED", failure={"code": "REVIEW"}, not_before=datetime.now(UTC) + timedelta(hours=1)))
    initial = queue_snapshot(db)
    originals = {ref: provider.location(ref).read_bytes() for ref in provider.uploads}
    batch_url = f"{BASE}/batches/{checks[0]}"
    user.role = "OPERATOR"
    granted = [
        "arm2_delete_mark", "arm2_delete_execute", "arm2_cp_view", "arm2_cp_edit",
        "arm2_document_view", "arm2_document_edit",
    ]
    for required in granted[1:]:
        user.permissions = [item for item in granted if item != required]
        db.commit()
        receipt = ok(c.get(batch_url))
        assert not receipt["can_retry"]
        if required.endswith("_view"):
            assert receipt["state"] == "RESTRICTED"
            assert not receipt["objects"] and not receipt["cleanup"]
            assert c.get(f"/api/arm2/history/DOCUMENT/{documents[0]}").status_code == 403
        assert c.post(batch_url + "/retry").status_code == 403
        assert queue_snapshot(db) == initial
        assert all(provider.location(ref).read_bytes() == data for ref, data in originals.items())

    other = User(login="synthetic-other", name="Synthetic other", active=True, role="ADMIN_OPERATOR",
                 password_hash=hasher.hash("synthetic-other-password"))
    db.add(other)
    db.commit()
    other_client = TestClient(c.app)
    auth = other_client.post("/api/auth/login", json={"login": other.login, "password": "synthetic-other-password"})
    assert auth.status_code == 200
    other_client.headers["X-CSRF-Token"] = other_client.cookies["csrf"]
    assert other_client.post(batch_url + "/retry").status_code == 404
    assert queue_snapshot(db) == initial

    user.permissions = granted
    db.commit()
    assert ok(c.get(batch_url))["can_retry"]
    queued = ok(c.post(batch_url + "/retry"))
    assert queued["state"] == "FILES_PENDING" and not queued["can_retry"]
    current = queue_snapshot(db)
    affected = {key for key, row in initial.items()
                if row["payload"]["batch_id"] == checks[0] and row["state"] == "FAILED"}
    assert len(affected) == 1
    assert {key for key in current if current[key] != initial[key]} == affected
    for key in affected:
        assert current[key] == {**initial[key], "state": "QUEUED", "failure": None, "not_before": None}

    # Revocation after authorization of the SQL deletion does not cancel durable work.
    user.permissions = ["arm2_delete_mark"]
    db.commit()
    monkeypatch.setattr(provider, "trash_exchange_file",
                        lambda ref, _digest: provider.location(ref).unlink(), raising=False)
    while work_once(engine(), {CLEANUP: clean_job}):
        pass
    assert all(queue_snapshot(db)[key]["state"] == "SUCCEEDED" for key in affected)
    assert not provider.location(provider.uploads[0]).exists()
    assert provider.location(provider.uploads[1]).read_bytes() == originals[provider.uploads[1]]


@pytest.mark.parametrize("change", ["version", "foreign_key", "json", "mark"])
def test_overlapping_transactions_invalidate_checked_set(qa, change):
    c, db, user, parents, document_kind, provider = qa
    url = create(qa, "veh")
    document_id = url.split("/")[-1]
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    original = provider.location(provider.uploads[0])
    contents = original.read_bytes()
    key = ("VEH", parents["veh"])
    mark(c, *key)
    mark(c, "DOCUMENT", document_id)
    checked = preview(c, key, ("DOCUMENT", document_id))
    assert checked["can_execute"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine().begin() as writer:
            pid = writer.scalar(text("SELECT pg_backend_pid()"))
            if change == "version":
                writer.execute(update(Vehicle).where(Vehicle.id == key[1]).values(version=Vehicle.version + 1))
            elif change == "foreign_key":
                writer.execute(Document.__table__.insert().values(
                    id="DOCUMENT-999999", parent_type="VEH", parent_id=key[1], kind_ref=document_kind,
                    name="Synthetic new reference",
                ))
            elif change == "json":
                writer.execute(BulkSession.__table__.insert().values(
                    id=str(uuid4()), user_id=user.id, source="Synthetic", payload={"plans": [{"target": key[1]}]},
                ))
            else:
                writer.execute(select(Vehicle.id).where(Vehicle.id == key[1]).with_for_update())
                writer.execute(delete(DeletionMark).where(DeletionMark.object_type == key[0],
                                                           DeletionMark.object_id == key[1]))
            pending = pool.submit(execute, c, checked["id"])
            deadline = monotonic() + 3
            with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as monitor:
                while monotonic() < deadline:
                    blocked = monitor.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE :pid=ANY(pg_blocking_pids(pid)))"), {"pid": pid})
                    if blocked:
                        break
                    sleep(0.02)
                else:
                    pytest.fail("The delete transaction did not overlap the change")
        assert pending.result(timeout=5).status_code == 409
    assert db.get(DeletionBatch, checked["id"]).executed_at is None
    assert db.get(Vehicle, key[1]) is not None and db.get(Document, document_id) is not None
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None
    assert not queue_snapshot(db)
    assert original.read_bytes() == contents


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
