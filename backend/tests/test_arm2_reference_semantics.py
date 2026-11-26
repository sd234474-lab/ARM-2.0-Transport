"""End-to-end evidence for registered ARM2 reference semantics."""

from copy import deepcopy
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import inspect, select
from test_arm2_bulk import accept as import_row
from test_arm2_bulk import book_bytes, edit, reopen, upload
from test_arm2_calibration_approval import letter, prepare
from test_arm2_deletion import BASE, execute, mark, ok, preview, queue_snapshot
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import qa as qa
from test_arm2_document_operations import operation, parent
from test_arm2_questionnaire_workflow import apply as apply_questionnaire
from test_arm2_questionnaire_workflow import b5 as b5
from test_arm2_questionnaire_workflow import choose as choose_questionnaire
from test_arm2_questionnaire_workflow import doc_kind, transport, workbook
from test_arm2_questionnaire_workflow import upload as upload_questionnaire

from app.arm2 import deletion_relations as relations
from app.arm2.bulk_models import BulkReceipt, BulkSession
from app.arm2.deletion_models import DeletionBatch, DeletionMark
from app.arm2.deletion_service import resolve_reference
from app.arm2.document_file_models import DocumentRevisionContext
from app.arm2.file_read_guard import attempt_lease
from app.arm2.history_models import HistoryEvent
from app.arm2.questionnaire_models import QuestionnaireReceipt
from app.db import engine


def test_registered_foreign_keys_match_real_schema(qa):
    policies = relations.deletion_policies()
    by_table = {model.__tablename__ for model in policies}
    expected = set(relations.reference_policies())
    catalog = inspect(engine()).get_multi_foreign_keys(schema="public")
    observed = {(table, tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for (_, table), fks in catalog.items() for fk in fks if fk["referred_table"] in by_table}
    assert observed == expected


def test_real_xlsx_import_and_replay_keep_receipt_and_history_after_delete(qa):
    c, db, user, _, _, _ = qa
    book = Workbook()
    book.active.append(("Госномер", "Рабочее имя"))
    book.active.append(("А701ВС86", "Для проверки истории"))
    draft = upload(c, book_bytes(book), "history.xlsx")
    recorded, body = import_row(c, draft)
    identifier = recorded["vehicle_id"]
    address = f"/api/arm2/bulk/sessions/{draft['id']}"
    assert ok(c.post(address + "/apply", json=body)) == recorded
    assert c.post(address + "/apply", json={**body, "confirm_owner": True}).status_code == 409
    reopened = reopen(c, draft)
    mark(c, "VEH", identifier)
    blocked = preview(c, ("VEH", identifier))
    assert not blocked["can_execute"]
    assert any(link["purpose"] == "OPEN_OPERATION" and draft["id"] in link["path"]
               for link in blocked["dependencies"] if link.get("path"))
    assert c.get(address + "/report").status_code == 200
    ok(c.post(address + "/finish", json={"revision": reopened["revision"]}))
    book.active.append(("А701ВС86", "Другое предложение"))
    same = upload(c, book_bytes(book), "same.xlsx")
    plan = same["plans"][0]
    keep = next(index for index, field in enumerate(plan["fields"]["name"]) if field.get("no_change"))
    same_result, _ = import_row(c, same, plan, {"name": keep})
    assert same_result["status"] == "SKIP" and same_result["event_id"] is None
    same_address = f"/api/arm2/bulk/sessions/{same['id']}"
    assert c.get(same_address + "/report").status_code == 200
    ok(c.post(same_address + "/finish", json={"revision": same["revision"]}))
    checked = preview(c, ("VEH", identifier))
    assert checked["can_execute"]
    assert any(link["purpose"] == "HISTORY" and not link["blocking"] for link in checked["dependencies"])
    ok(execute(c, checked["id"]))
    assert c.get(f"/api/arm2/vehicles/{identifier}").status_code == 404
    assert c.post(address + "/apply", json=body).status_code == 404
    historic = ok(c.get(f"/api/arm2/bulk/protocols?session_id={draft['id']}"))[0]["results"][0]
    assert historic["current_object"]["state"] == "DELETED"
    assert {key: value for key, value in historic.items() if key != "current_object"} == recorded
    assert db.get(BulkReceipt, body["command_id"]).vehicle_id == identifier
    assert ok(c.get(f"/api/arm2/history/events/{recorded['event_id']}"))["object_id"] == identifier
    assert ok(c.get(f"/api/arm2/history/events/{historic['current_object']['event_id']}"))["operation"] == "DELETED"
    assert resolve_reference(db, user, "VEH", "VEH-999999") == {"state": "UNKNOWN"}
    same_receipt = ok(c.get(f"/api/arm2/bulk/protocols?session_id={same['id']}"))[0]["results"][0]
    assert same_receipt.pop("current_object")["state"] == "DELETED"
    assert same_receipt == same_result


def test_only_typed_plan_refs_are_live_and_unknown_shape_is_rejected(qa):
    c, db, _, parents, _, _ = qa
    identifier = parents["veh"]
    draft = upload(c, f"Госномер\tРабочее имя\nА125ВС86\t{identifier}")
    row = db.get(BulkSession, draft["id"])
    plan = {**row.payload["plans"][0], "similar": [{"id": identifier, "plate": "А123ВС86"}]}
    row.payload = {**row.payload, "originals": {identifier: {"name": identifier}}, "plans": [plan]}
    db.commit()
    mark(c, "VEH", identifier)
    assert preview(c, ("VEH", identifier))["can_execute"]
    row.payload = {**row.payload, "plans": [{**plan, "unexpected": identifier}]}
    db.commit()
    response = c.post(BASE + "/preview", json={"objects": [{"object_type": "VEH", "object_id": identifier}]})
    assert response.status_code == 409 and "arm2_bulk_sessions.payload.plans" in response.json()["detail"]
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None


@pytest.mark.parametrize("damage", ["unknown-version", "legacy-target", "unknown-option", "bad-index", "bad-typed-id"])
def test_unknown_bulk_shapes_stop_preview_execute_and_reopen_without_mutation(qa, damage):
    c, db, _, parents, _, _ = qa
    identifier = parents["veh"]
    selected = ("VEH", identifier)
    marked = mark(c, *selected)
    checked = preview(c, selected)
    assert checked["can_execute"]
    draft = upload(c, "Госномер\tРабочее имя\nА812ВС86\tОтдельная машина")
    record = db.get(BulkSession, draft["id"])
    payload = deepcopy(record.payload)
    plan = payload["plans"][0]
    if damage in ("unknown-version", "legacy-target"):
        payload.pop("schema")
        payload["plans"] = [{"key": "p1", "fields": {}, "target": {"type": "VEH", "id": identifier}}]
        if damage == "unknown-version":
            payload["schema"] = "ARM_BULK_V999"
    if damage == "unknown-option":
        plan["fields"]["name"][0]["target"] = {"type": "VEH", "id": identifier}
    if damage == "bad-index":
        payload["decisions"] = {plan["key"]: {"name": False}}
    if damage == "bad-typed-id":
        plan["fields"]["type_id"] = [{**plan["fields"]["name"][0], "value": "VEH-000001"}]
    record.payload = payload
    revision = record.revision
    db.commit()
    selected_body = {"objects": [{"object_type": selected[0], "object_id": selected[1]}]}
    assert c.post(BASE + "/preview", json=selected_body).status_code == 409
    assert execute(c, checked["id"]).status_code == 409
    workspace = f"/api/arm2/bulk/sessions/{draft['id']}"
    draft_keys = ("headers", "rows", "mapping", "excluded", "clears", "decisions", "confirmations")
    assert c.get(workspace).status_code == 409
    assert c.get(workspace + "/report").status_code == 409
    assert c.put(workspace, json={"revision": revision, **{k: draft[k] for k in draft_keys}}).status_code == 409
    assert c.post(workspace + "/apply", json={"command_id": str(uuid4()), "preview_id": draft["preview_id"],
                                              "row_key": plan["key"], "fields": {"name": 0}}).status_code == 409
    assert c.post(workspace + "/finish", json={"revision": revision}).status_code == 409
    current = db.get(BulkSession, draft["id"], populate_existing=True)
    assert current.payload == payload and current.revision == revision
    assert c.get(f"/api/arm2/vehicles/{identifier}").status_code == 200
    assert db.get(DeletionMark, selected).command_id == marked["mark"]
    assert db.get(DeletionBatch, checked["id"]).executed_at is None
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None
    assert not queue_snapshot(db)
    if damage == "unknown-version":
        cleanup = f"/api/arm2/bulk/cleanup/{draft['id']}"
        assert c.post(cleanup, json={"confirm_session": str(uuid4())}).status_code == 422
        assert ok(c.post(cleanup, json={"confirm_session": draft["id"]}))["cleaned"] == draft["id"]
        assert c.get(workspace).status_code == 404 and preview(c, selected)["can_execute"]


def test_only_selected_bulk_type_and_owner_options_block_deletion(qa):
    c, _, _, parents, _, _ = qa
    typ = ok(c.post("/api/arm2/types", json={
        "name": "Тип для сверки решений", "plate_category": "Автомобильный", "requires_sts": True,
        "requires_calibration": False, "requires_wialon": False,
    }), 201)["id"]
    cp = parents["cp"]
    draft = upload(c, f"а\tб\tв\tг\nА813ВС86\t{typ}\tCP\t{cp}")
    draft = edit(c, draft, mapping=["plate_number", "type_id", "owner_kind", "owner_id"])
    plan = draft["plans"][0]
    assert plan["fields"]["type_id"][0]["value"] == typ and plan["fields"]["owner"][0]["value"] == ["CP", cp]
    mark(c, "TYPE", typ)
    mark(c, "CP", cp)
    selection = (("TYPE", typ), ("CP", cp))
    assert preview(c, *selection)["can_execute"]
    draft = edit(c, draft, decisions={plan["key"]: {"type_id": 0}})
    blocked = preview(c, *selection)
    assert not blocked["can_execute"]
    assert {item["target_id"] for item in blocked["dependencies"] if item["blocking"]} == {typ}
    edit(c, draft, decisions={plan["key"]: {"type_id": 0, "owner": 0}})
    assert {item["target_id"] for item in preview(c, *selection)["dependencies"] if item["blocking"]} == {typ, cp}


def test_partial_b5_holds_selected_counterparty_then_keeps_immutable_receipt(b5):
    c, db, _, parents, _, _ = b5
    identifier = parents["cp"]
    draft = upload_questionnaire(c, workbook({"B18": "+7 (900) 101-20-30"}))
    draft = choose_questionnaire(c, draft, cp_target=identifier, cp_fields=["phone"])
    result, body = apply_questionnaire(c, draft, without_documents=True)
    assert result["event_ids"]
    before = db.get(QuestionnaireReceipt, body["command_id"]).result
    db.rollback()
    mark(c, "CP", identifier)
    blocked = preview(c, ("CP", identifier))
    assert not blocked["can_execute"]
    assert any(row["purpose"] == "OPEN_OPERATION" for row in blocked["dependencies"])
    ok(c.delete(f"/api/arm2/questionnaires/{draft['id']}"))
    checked = preview(c, ("CP", identifier))
    assert checked["can_execute"]
    ok(execute(c, checked["id"]))
    protocol = ok(c.get("/api/arm2/questionnaires/receipts"))[0]
    assert protocol["event_ids"] == result["event_ids"]
    assert protocol["objects"][0]["current_object"]["state"] == "DELETED"
    assert db.get(QuestionnaireReceipt, body["command_id"]).result == before
    repeated, _ = apply_questionnaire(c, draft, body=body)
    assert repeated == protocol


def test_complete_questionnaire_receipt_reopens_after_deleting_the_explicit_set(b5):
    c, db, _, parents, _, disk = b5
    kind = doc_kind(c)
    original = pdf("original attached to accepted questionnaire")
    form = workbook({"B18": "+7 (900) 101-21-30", "B20": "Тестовый Представитель"},
                    [{"E": "A123BC86", "K": "Модель из анкеты", "P": "sts.pdf"}])
    draft = upload_questionnaire(c, transport(form, {"sts.pdf": original}), True)
    draft = choose_questionnaire(
        c, draft, cp_target=parents["cp"], cp_fields=["phone"],
        representatives={"0": {"target": "new", "fields": ["name"]}},
        vehicles={"v:5": {"include": True, "fields": ["model"]}},
        documents={"v:5:sts": {"kind_id": kind, "accept": True}},
    )
    result, command = apply_questionnaire(c, draft)
    subjects = [(item["kind"], item.get("id") or item["document_id"]) for item in result["objects"]]
    assert {subject[0] for subject in subjects} == {"CP", "CPREP", "VEH", "DOCUMENT"}
    before = db.get(QuestionnaireReceipt, command["command_id"]).result
    db.rollback()
    assert len(disk.uploads) == 1 and disk.location(disk.uploads[0]).read_bytes() == original
    for kind, identifier in subjects:
        mark(c, kind, identifier)
    checked = preview(c, *subjects)
    assert checked["can_execute"]
    ok(execute(c, checked["id"]))
    protocol = ok(c.get(f"/api/arm2/questionnaires/{draft['id']}"))["receipt"]
    assert protocol["event_ids"] == result["event_ids"]
    assert {item["current_object"]["state"] for item in protocol["objects"]} == {"DELETED"}
    for event_id in protocol["event_ids"]:
        assert ok(c.get(f"/api/arm2/history/events/{event_id}"))["correlation_id"] == command["command_id"]
    repeated, _ = apply_questionnaire(c, draft, body=command)
    assert repeated == protocol and len(disk.uploads) == 1
    assert db.get(QuestionnaireReceipt, command["command_id"]).result == before


@pytest.mark.parametrize("kind", ["CP", "ORG", "VEH"])
def test_move_then_delete_old_parent_keeps_the_revision_and_bytes(qa, kind):
    c, db, _, parents, _, disk = qa
    token = kind.lower()
    initial = create(qa, token)
    old = parents[token]
    original = pdf("original remains available")
    ok(accept(c, initial, 1, [stage(c, initial, original)]))
    revision = bundle(c, initial)["revisions"][0]
    new = parent(c, token)
    ok(operation(c, initial, "move", 2, target_type=kind, target_id=new))
    mark(c, kind, old)
    mark(c, kind, new)
    assert not preview(c, (kind, new))["can_execute"]
    checked = preview(c, (kind, old))
    assert checked["can_execute"] and any(link["purpose"] == "HISTORY" for link in checked["dependencies"])
    assert ok(execute(c, checked["id"]))["state"] == "COMPLETED"
    moved = f"/api/arm2/{token}/{new}/documents/{revision['document_id']}"
    historic = bundle(c, moved)["revisions"][0]
    assert historic["parent_id"] == old and historic["parent_current_object"]["state"] == "DELETED"
    assert db.get(DocumentRevisionContext, revision["id"]).parent_id == old
    file_id = revision["files"][0]["id"]
    assert c.get(f"{moved}/revisions/{revision['id']}/files/{file_id}/download").content == original
    assert disk.location(disk.uploads[0]).read_bytes() == original


def test_file_acceptance_lease_then_historical_replay(qa):
    c, db, _, _, _, disk = qa
    url = create(qa, "veh")
    identifier = url.rsplit("/", 1)[1]
    candidate = stage(c, url, pdf("command evidence"))
    command = str(uuid4())
    result = ok(accept(c, url, 1, [candidate], command))
    mark(c, "DOCUMENT", identifier)
    with attempt_lease(db, command):
        blocked = c.post(BASE + "/preview", json={"objects": [
            {"object_type": "DOCUMENT", "object_id": identifier},
        ]})
        assert blocked.status_code == 409
    checked = preview(c, ("DOCUMENT", identifier))
    assert checked["can_execute"]
    ok(execute(c, checked["id"]))
    archived = db.get(DeletionBatch, checked["id"]).artifacts["arm2_document_file_acceptances"][command]
    assert archived["result"] == result and "session_hash" not in archived and "plan" not in archived
    replay = ok(accept(c, url, 1, [candidate], command))
    assert replay.pop("current_object")["state"] == "DELETED" and replay == result
    assert accept(c, url, 2, [candidate], command).status_code == 409
    assert len(disk.uploads) == 1


def test_completed_approval_remains_replayable_from_its_ledger(qa):
    c, _, _, _, _, disk = qa
    body, response = letter(c, prepare(qa))
    saved = ok(response, 201)
    identifier = saved["request_id"]
    request = ok(c.get(f"/api/arm2/approvals/{identifier}"))
    keys = [("APPROVAL", identifier), *(("APPROVAL-ITEM", item["id"]) for item in request["decisions"])]
    for key in keys:
        mark(c, *key)
    checked = preview(c, *keys)
    assert checked["can_execute"]
    ok(execute(c, checked["id"]))
    uploads = len(disk.uploads)
    replay = ok(c.post("/api/arm2/approvals/letter", json=body), 201)
    assert replay.pop("current_object")["state"] == "DELETED" and replay == saved
    assert c.post("/api/arm2/approvals/letter", json={**body, "contract_number_1": "Mismatch"}).status_code == 409
    assert len(disk.uploads) == uploads
