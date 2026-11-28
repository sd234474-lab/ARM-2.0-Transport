"""REL-ARCH-04 ingress, staged relation proof, receipt and protected cleanup."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_arm2_bulk import edit as edit_bulk
from test_arm2_bulk import upload as upload_bulk
from test_arm2_calibration_approval import letter, prepare
from test_arm2_deletion import execute, mark, preview
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa
from test_arm2_document_operations import operation
from test_arm2_questionnaire_workflow import (
    apply,
    check,
    choose,
    doc_kind,
    reopen,
    snapshot,
    transport,
    upload,
    workbook,
)
from test_arm2_questionnaire_workflow import b5 as b5
from test_arm2_vehicle_links import command as assign_owner
from test_arm2_vehicle_links import current

from app.arm2 import document_file_storage as files
from app.arm2.approval_models import ApprovalRequest
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireReceipt, QuestionnaireUpload
from app.arm2.questionnaire_parse import EXPORT_VERSION, VEHICLE_SELECTOR_HEADER, VEHICLES
from app.arm2.reference_paths import verify_approval_snapshots
from app.arm2.schemas import DocumentOperation, OperationalOwnerCommand, VehicleCreate


@pytest.mark.parametrize("version", ("ARM_IMPORT_V2", EXPORT_VERSION))
def test_explicit_id_never_falls_back_to_a_matching_plate(b5, version):
    c, db, _, parents, *_ = b5
    vehicle = current(c, parents["veh"])
    identifier = vehicle["id"]

    def header(book):
        if version == EXPORT_VERSION:
            book[VEHICLES]["B4"] = VEHICLE_SELECTOR_HEADER

    before = snapshot(db)
    for bad in (identifier + "\t", identifier + "-BAD", " " + identifier, identifier.replace("0", "０"),
                parents["cp"], "VEH-999999"):
        draft = upload(c, workbook({"B3": version}, [{"B": bad, "E": vehicle["plate_number"], "F": "Proposed"}], header))
        row = draft["comparison"]["vehicles"][0]
        assert row["id"] is None and row["errors"], bad
        db.expire_all()
        assert db.get(QuestionnaireDraft, draft["id"]).payload["vehicles"][0]["id"] == bad
        draft = choose(c, draft, cp_target=parents["cp"], vehicles={row["key"]: {"include": True}})
        apply(c, draft, 422)
    assert snapshot(db) == before


def test_file_dto_and_bulk_mapped_input_keep_rejected_ids_raw(qa):
    c, db, _, parents, *_ = qa
    path = create(qa)
    check(accept(c, path, 1, [stage(c, path, pdf("REL-ARCH-04"))]))
    identifier = bundle(c, path)["revisions"][0]["files"][0]["id"]
    vehicle = current(c, parents["veh"])
    before = snapshot(db)
    for bad in (" " + identifier, identifier + "\n", identifier + "\t"):
        body = {"expected_version": 2, "idempotency_key": str(uuid4())}
        assert c.post(path + "/accept", json={**body, "files": [{"file_id": bad}]}).status_code == 422
        assert c.post(path + "/corrections", json={**body, "file_id": bad}).status_code == 422
    table = upload_bulk(c, f"REF\tPlate\n\"{vehicle['id']}\t\"\t{vehicle['plate_number']}")
    draft = edit_bulk(c, table, mapping=["id", "plate_number"])
    assert draft["plans"][0]["action"] == "CONFLICT" and draft["plans"][0]["errors"]
    assert snapshot(db) == before


def test_shared_command_fields_keep_raw_ids_for_validation():
    assert DocumentOperation(expected_version=1, target_id="VEH-000001\n").target_id == "VEH-000001\n"
    owner = OperationalOwnerCommand(expected_version=1, target_kind="CP", target_id=" CP-000001",
                                    expected_owner_kind=None, expected_owner_id=None)
    assert owner.target_id == " CP-000001"
    vehicle = VehicleCreate(name="  Name  ", plate_number="A123BC86", type_id="TYPE-000001\t")
    assert vehicle.type_id == "TYPE-000001\t" and vehicle.name == "Name"


def test_prepared_document_and_file_scope_checks_saved_plan_and_keeps_originals(b5):
    c, db, _, parents, kind, disk = b5
    source = create(b5, "veh")
    check(accept(c, source, 1, [stage(c, source, pdf("source"))]))
    other = check(c.post("/api/arm2/vehicles", json={"name": "Foreign", "plate_number": "А919ВС86"}), 201)
    link = f"/api/arm2/veh/{other['id']}/documents"
    foreign = check(c.post(link, json={"kind_ref": kind, "name": "Foreign"}), 201)
    link += "/" + foreign["id"]
    check(accept(c, link, 1, [stage(c, link, pdf("foreign"))]))
    foreign_file = bundle(c, link)["revisions"][0]["files"][0]["id"]
    draft = upload(c, transport(workbook(rows=[{"E": "А123ВС86", "P": "updated.pdf"}]),
                                {"updated.pdf": pdf("update")}), True)
    draft = choose(c, draft, cp_target=parents["cp"], documents={
        "v:5:sts": {"kind_id": kind, "document_id": source.split("/")[-1], "accept": True},
    })
    disk.fail_at = len(disk.uploads)
    _, command = apply(c, draft, 503)
    disk.fail_at = None
    mark(c, "VEH", parents["veh"])
    preview(c, ("VEH", parents["veh"]))  # Valid stale-safe plan remains a blocker.
    # Preflight an unrelated marked object before any malformed logical row exists.
    target = ("ORG", parents["org"])
    mark(c, *target)
    checked = preview(c, target)
    assert checked["can_execute"]
    db.rollback()
    stored = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
    staging = deepcopy(stored.staging)
    before = snapshot(db)
    mutations = (
        {"document_id": foreign["id"]},
        {"files": [{**staging["groups"][0]["files"][0], "id": foreign_file, "existing": True}]},
        {"replacements": [foreign_file]},
        {"expected_version": staging["groups"][0]["expected_version"] + 1},
    )
    for patch in mutations:
        invalid = deepcopy(staging)
        invalid["groups"][0].update(patch)
        stored.staging = invalid
        db.commit()
        assert c.post("/api/arm2/deletion/preview", json={"objects": [
            {"object_type": target[0], "object_id": target[1]},
        ]}).status_code == 409
        assert execute(c, checked["id"]).status_code == 409
        assert snapshot(db) == before
        stored.staging = deepcopy(staging)
        db.commit()
    assert preview(c, target)["can_execute"]
    result, _ = apply(c, draft, body=command)
    assert not result["no_op"]
    assert len(bundle(c, source)["revisions"]) == 2
    assert len(bundle(c, link)["revisions"]) == 1


def test_partial_acceptance_records_selected_noop_row_without_exposing_private_proof(b5):
    c, db, _, parents, *_ = b5
    check(assign_owner(c, current(c, parents["veh"]), ("CP", parents["cp"])))
    vehicle = current(c, parents["veh"])
    draft = upload(c, workbook(rows=[{"E": vehicle["plate_number"]}, {"I": "А919ВС86", "J": "Unselected"}]))
    draft = choose(c, draft, cp_target=parents["cp"], vehicles={
        "v:5": {"include": True}, "v:6": {"include": False},
    })
    before = snapshot(db)
    receipt, body = apply(c, draft, without_documents=True)
    assert receipt["no_op"] and snapshot(db) == before
    assert "row_bindings_v1" not in receipt
    stored = db.get(QuestionnaireReceipt, body["command_id"])
    assert stored.result["row_bindings_v1"] == [{
        "v": 1, "row_key": "v:5", "vehicle_id": vehicle["id"], "version_after": vehicle["version"],
        "command_id": body["command_id"], "draft_id": draft["id"],
    }]
    assert reopen(c, draft)["receipt"] == receipt
    assert apply(c, draft, body=body)[0] == receipt


def test_cancel_commits_while_preview_copies_and_cleanup_retries(b5, monkeypatch):
    c, db, *_ = b5
    data = pdf("REL-ARCH C09")
    draft = upload(c, transport(workbook(), {"preview.pdf": data}), True)
    identifier = draft["id"]
    token = db.scalar(select(QuestionnaireUpload.id).where(QuestionnaireUpload.draft_id == identifier))
    original = files.private_path(token)
    entered, release = Event(), Event()
    copy = files.staged_preview

    def pause(stage):
        entered.set()
        assert release.wait(10)
        return copy(stage)

    monkeypatch.setattr(files, "staged_preview", pause)
    endpoint = "/api/arm2/questionnaires/" + identifier
    path = endpoint + "/files/" + token + "/preview"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c.get, path)
        try:
            assert entered.wait(10)
            result = check(c.delete(endpoint))
            assert result["status"] == "CANCELLED" and result["cleanup"]["pending"]
            assert original.read_bytes() == data
            assert reopen(c, draft)["status"] == "CANCELLED"
        finally:
            release.set()
        response = future.result(timeout=10)
        assert response.status_code == 200 and response.content == data
    assert c.get(path).status_code == 409 and original.exists()
    repeated = check(c.delete(endpoint))
    assert repeated["status"] == "CANCELLED" and repeated["cleanup"]["pending"] == 0
    assert not original.exists()
    db.expire_all()
    assert db.get(QuestionnaireUpload, token).prepared["cleanup_v1"]["complete"] is True


def test_failed_remote_cleanup_keeps_terminal_receipt_and_retries_only_own_original(b5, monkeypatch):
    c, db, _, parents, _, disk = b5
    draft = upload(c, transport(workbook({"B28": "company.pdf"}, rows=[{"E": "А123ВС86", "P": "sts.pdf"}]),
                                {"sts.pdf": pdf("selected"), "company.pdf": pdf("orphan")}), True)
    company, sts = doc_kind(c, "Карточка предприятия", "CP"), doc_kind(c)
    draft = choose(c, draft, cp_target=parents["cp"], documents={
        "company": {"kind_id": company, "accept": True}, "v:5:sts": {"kind_id": sts, "accept": True},
    })
    disk.fail_at = 1
    apply(c, draft, 503)
    abandoned = disk.uploads[0]
    disk.fail_at = None
    draft = choose(c, draft, documents={
        "company": {"kind_id": company, "accept": False}, "v:5:sts": {"kind_id": sts, "accept": True},
    })
    trash = disk.trash_exchange_file

    def unavailable(path, digest):
        assert path == abandoned
        raise RuntimeError("Synthetic provider failure")

    monkeypatch.setattr(disk, "trash_exchange_file", unavailable)
    receipt, _ = apply(c, draft)
    assert receipt["event_ids"] and reopen(c, draft)["receipt"] == receipt
    accepted = disk.uploads[1]
    assert disk.location(accepted).read_bytes() == pdf("selected")
    assert disk.location(abandoned).read_bytes() == pdf("orphan")
    assert draft["id"] in {row["id"] for row in check(c.get("/api/arm2/questionnaires/cleanup"))}
    monkeypatch.setattr(disk, "trash_exchange_file", trash)
    assert check(c.post(f"/api/arm2/questionnaires/{draft['id']}/cleanup"))["pending"] == 0
    assert not disk.location(abandoned).exists()
    assert disk.location(accepted).read_bytes() == pdf("selected")
    db.expire_all()
    assert reopen(c, draft)["receipt"] == receipt


def test_approval_uses_scope_at_issuance_between_two_document_moves(qa):
    c, db, *_ = qa
    first, source, _, _ = prepare(qa)[0]
    document = check(c.get(source))
    old_revision = bundle(c, source)["current_revision_id"]
    type_id = current(c, first)["type_id"]
    second, third = [check(c.post("/api/arm2/vehicles", json={
        "name": label, "plate_number": plate, "type_id": type_id,
    }), 201) for label, plate in (("Second", "А919ВС86"), ("Third", "А929ВС86"))]
    moved = check(operation(c, source, "move", document["version"], target_type="VEH", target_id=second["id"]))
    middle = f"/api/arm2/veh/{second['id']}/documents/{document['id']}"
    assert bundle(c, middle)["current_revision_id"] == old_revision
    check(c.post(f"/api/arm2/veh/{second['id']}/calibration/confirm", json={
        "command_id": str(uuid4()), "vehicle_version": second["version"], "document_id": document["id"],
        "document_version": moved["version"], "revision_id": old_revision, "plate_number": second["plate_number"],
        "volume_m3": 27.0,
    }))
    _, response = letter(c, [(second["id"],)])
    identifier = check(response, 201)["request_id"]
    url = f"/api/arm2/approvals/{identifier}"
    before = check(c.get(url))
    check(operation(c, middle, "move", moved["version"], target_type="VEH", target_id=third["id"]))
    db.rollback()
    request = db.get(ApprovalRequest, identifier)
    assert verify_approval_snapshots(db, request.vehicles_snapshot)
    wrong = [{**request.vehicles_snapshot[0], "vehicle_id": third["id"]}]
    with pytest.raises(ValueError, match="истории родителя"):
        verify_approval_snapshots(db, wrong)
    missing = [{**request.vehicles_snapshot[0], "document_version": moved["version"] + 10}]
    assert verify_approval_snapshots(db, missing) is False
    assert check(c.get(url)) == before
    mark(c, "VEH", second["id"])
    result = preview(c, ("VEH", second["id"]))
    assert not result["can_execute"] and any(link["purpose"] == "ACTIVE_DOMAIN" for link in result["dependencies"])
