"""End-to-end evidence for registered ARM2 reference semantics."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
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
from test_arm2_document_operations import correct, operation, parent
from test_arm2_questionnaire_workflow import apply as apply_questionnaire
from test_arm2_questionnaire_workflow import b5 as b5
from test_arm2_questionnaire_workflow import choose as choose_questionnaire
from test_arm2_questionnaire_workflow import doc_kind, transport, workbook
from test_arm2_questionnaire_workflow import reopen as reopen_questionnaire
from test_arm2_questionnaire_workflow import upload as upload_questionnaire

from app.arm2 import deletion_relations as relations
from app.arm2 import document_file_storage as file_storage
from app.arm2 import reference_paths as refs
from app.arm2.approval_models import ApprovalRequest
from app.arm2.bulk_models import BulkReceipt, BulkSession
from app.arm2.deletion_models import DeletionBatch, DeletionMark
from app.arm2.deletion_service import resolve_reference
from app.arm2.document_file_models import DocumentRevisionContext, FileAcceptance
from app.arm2.file_read_guard import attempt_lease
from app.arm2.history_models import HistoryEvent
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireReceipt, QuestionnaireUpload
from app.db import engine

MISSING = object()
INVALID_ID = [
    pytest.param(None, id="null"), pytest.param("", id="empty"), pytest.param("new", id="new"),
    pytest.param("$CP", id="owner-placeholder"), pytest.param("WRONG-000001", id="wrong-prefix"),
    pytest.param(True, id="boolean"), pytest.param(1, id="number"), pytest.param({}, id="object"),
    pytest.param([], id="list"),
]


def set_or_remove(row, key, value):
    if value is MISSING:
        row.pop(key)
    else:
        row[key] = value


def preview_row(*, cp=None, vehicle=None, document=None, choices=None, staging=None):
    comparison = {}
    if cp is not None:
        comparison["cp"] = cp
    if vehicle is not None:
        comparison["vehicles"] = [vehicle]
    if document is not None:
        comparison["documents"] = [document]
    return SimpleNamespace(choices=choices or {}, preview=comparison, payload={}, staging=staging or {})


def vehicle_row():
    return {"key": "v:5", "id": None, "version": None, "plate_number": "А123ВС86", "withdrawal": False,
            "errors": [], "fields": {"owner": {"current": None, "proposed": None}}}


def cp_row(identifier=None):
    fields = {name: {"proposed": value, "selected": True, "error": None}
              for name, value in (("name", "Синтетический CP"), ("type", "ЮЛ"))}
    return {"key": "CP", "target": identifier or "new", "id": identifier, "version": 1 if identifier else None,
            "error": None, "fields": fields}


def document_preview():
    return {"key": "v:5:sts", "selected": False, "parent_key": "v:5", "parent_type": "VEH",
            "upload_id": "b" * 64, "current_files": []}


def file_plan(*, existing=False):
    return {"id": "FILE-000001", "original_upload_name": "qa.pdf", "stored_name": "qa.pdf", "size": 12,
            "sha256": "a" * 64, "storage_ref": "synthetic/qa.pdf", "media_type": "application/pdf",
            "extension": "pdf", "existing": existing}


def document_group():
    return {"parent_type": "CP", "parent_key": "CP", "parent_id": None, "kind_id": "DOC-KIND-000001",
            "document_id": None, "document_name": "Тест", "expected_version": None, "files": [], "replacements": []}


def staging_with_cp(group, *, existing_parent=False):
    cp = cp_row("CP-000001" if existing_parent else None)
    return refs.staging_group(group, comparison={"cp": cp},
                              choices={"cp_target": cp["target"], "cp_fields": ["name", "type"]})


def existing_document_group():
    return {**document_group(), "document_id": "DOCUMENT-000001", "expected_version": 1, "parent_id": "CP-000001"}


def acceptance_plan():
    return {"revision_id": "DOCUMENT-REV-000001", "number": 1, "noop": False, "files": [], "uploaded": []}


def approval_vehicle():
    return {"vehicle_id": "VEH-000001", "document_id": "DOCUMENT-000001", "revision_id": "DOCUMENT-REV-000001",
            "document_version": 1, "vehicle_version": 1, "name": "Тест", "plate_number": "А123ВС86",
            "volume_m3": 12, "file_ids": []}


def typed_list_path(path, value):
    if path == "response_file_ids":
        data = {"response_file_ids": [], "vehicles_snapshot": [approval_vehicle()]}
        set_or_remove(data, path, value)
        return refs.approval_snapshot(SimpleNamespace(**data))
    if path == "snapshot.file_ids":
        vehicle = approval_vehicle()
        set_or_remove(vehicle, "file_ids", value)
        return refs.snapshot_references([vehicle], "vehicles_snapshot")
    if path == "staging.replacements":
        group = existing_document_group()
        set_or_remove(group, "replacements", value)
        return staging_with_cp(group, existing_parent=True)
    plan = acceptance_plan()
    set_or_remove(plan, "uploaded", value)
    return refs.file_acceptance(SimpleNamespace(plan=plan))


@pytest.mark.parametrize("path", ["response_file_ids", "snapshot.file_ids", "staging.replacements", "plan.uploaded"])
@pytest.mark.parametrize("value", INVALID_ID)
def test_strict_typed_lists_reject_every_invalid_element(path, value):
    with pytest.raises(ValueError):
        typed_list_path(path, [value])


@pytest.mark.parametrize("path", ["response_file_ids", "snapshot.file_ids", "staging.replacements", "plan.uploaded"])
def test_strict_typed_lists_require_collection_and_accept_empty_and_valid(path):
    for value in (MISSING, None, "", True, 3, {}):
        with pytest.raises((ValueError, KeyError, AttributeError)):
            typed_list_path(path, value)
    assert not any(r[0] == "FILE" for r in typed_list_path(path, []))
    assert any(r[:2] == ("FILE", "FILE-000001") for r in typed_list_path(path, ["FILE-000001"])) == (path != "plan.uploaded")


@pytest.mark.parametrize("value", INVALID_ID)
def test_required_typed_scalar_and_owner_pair_reject_invalid_values(value):
    with pytest.raises(ValueError):
        refs.typed([], "CP", value, "required")
    with pytest.raises(ValueError):
        refs.typed_owner([], ["CP", value], "owner")
    if value is not None:
        with pytest.raises(ValueError):
            refs.typed([], "CP", value, "optional", nullable=True)


def test_null_and_placeholders_are_only_allowed_for_explicit_cases():
    result = []
    refs.typed(result, "CP", None, "optional", nullable=True)
    refs.typed(result, "CP", "new", "cp", placeholders=("new",))
    refs.typed_owner(result, None, "owner")
    refs.typed_owner(result, ["CP", "$CP"], "owner", new_cp=True)
    for malformed in ([None, "CP-000001"], ["ORG", "$CP"], ["CP"], {"kind": "CP"}, "CP-000001"):
        with pytest.raises(ValueError):
            refs.typed_owner(result, malformed, "owner", new_cp=True)
    refs.typed_owner(result, ["CP", "CP-000001"], "owner")
    assert result == [("CP", "CP-000001", "owner")]


@pytest.mark.parametrize("field", ["value", "current", "original"])
def test_bulk_options_allow_nullable_type_and_owner_in_all_three_columns(field):
    option = {"value": None, "current": None, "original": None, "label": "", "current_label": "",
              "has_original": False, "error": None, "conflict": False, "clear": False, "sources": [2]}
    assert refs.bulk_option("type_id", option, 1) == []
    assert refs.bulk_option("owner", option, 1) == []
    for name, valid, invalid in (("type_id", "TYPE-000001", ""), ("owner", ["ORG", "ORG-000001"], ["CP", None])):
        refs.bulk_option(name, {**option, field: valid}, 1)
        for value in (MISSING, invalid):
            damaged = dict(option)
            set_or_remove(damaged, field, value)
            with pytest.raises(ValueError):
                refs.bulk_option(name, damaged, 1)


def test_bulk_target_is_nullable_in_create_and_checked_in_update():
    plan = {"key": "v", "sources": [2], "errors": [], "fields": {}, "action": "CREATE", "vehicle_id": None,
            "version": None, "matched": None, "similar": [], "has_snapshot": False}
    assert refs.bulk_plan(plan, 1)[1] == []
    matched = {"name": "Тест", "plate_number": "А123ВС86"}
    assert refs.bulk_plan({**plan, "action": "UPDATE", "vehicle_id": "VEH-000001", "version": 1, "matched": matched}, 1)[1]
    for update in ({"action": "UPDATE"}, {"version": 1}, {"vehicle_id": ""}, {"vehicle_id": "CP-000001"}):
        with pytest.raises(ValueError):
            refs.bulk_plan({**plan, **update}, 1)


@pytest.mark.parametrize("value", [MISSING, None, "", "new", "CP-000001"])
def test_cp_and_representative_choice_absence_and_known_placeholders(value):
    choices = {"cp_target": None, "representatives": {"0": {"target": None}}}
    set_or_remove(choices, "cp_target", value)
    rep = "CPREP-000001" if value == "CP-000001" else value
    set_or_remove(choices["representatives"]["0"], "target", rep)
    extracted = refs.questionnaire_selection(preview_row(choices=choices))
    assert len(extracted) == (2 if value == "CP-000001" else 0)


@pytest.mark.parametrize("field", ["kind_id", "document_id", "revision_id", "replace_file_id", "parent_id"])
def test_preview_document_optional_ids_respect_only_choice_placeholders(field):
    document = document_preview()
    assert refs.questionnaire_selection(preview_row(document=document)) == []
    assert refs.questionnaire_selection(preview_row(document={**document, field: None})) == []
    for invalid in ("", "new", "$CP", "ORG-000001", True, 1, {}, []):
        with pytest.raises(ValueError):
            refs.questionnaire_selection(preview_row(document={**document, field: invalid}))
    if field != "parent_id":
        choice = {"accept": False, field: ""}
        assert refs.questionnaire_selection(preview_row(choices={"documents": {"v:5:sts": choice}})) == []
        choice[field] = "new"
        if field == "document_id":
            assert refs.questionnaire_selection(preview_row(choices={"documents": {"v:5:sts": choice}})) == []
        else:
            with pytest.raises(ValueError):
                refs.questionnaire_selection(preview_row(choices={"documents": {"v:5:sts": choice}}))


@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_current_file_id_and_new_staging_kind_are_required(value):
    current_file = {"id": "FILE-000001"}
    set_or_remove(current_file, "id", value)
    document = {**document_preview(), "current_files": [current_file]}
    with pytest.raises((ValueError, KeyError)):
        refs.questionnaire_selection(preview_row(document=document))  # Validate even the unselected proposal.
    group = document_group()
    set_or_remove(group, "kind_id", value)
    with pytest.raises((ValueError, KeyError)):
        staging_with_cp(group)


def test_document_preview_and_staging_require_lists_in_full_structures():
    short = {"key": "v:5:sts", "parent_key": None, "selected": False}
    assert refs.questionnaire_selection(preview_row(document=short)) == []
    for value in (MISSING, None, "", True, {}):
        document = document_preview()
        set_or_remove(document, "current_files", value)
        with pytest.raises((ValueError, KeyError)):
            refs.questionnaire_selection(preview_row(document=document))
        group = document_group()
        set_or_remove(group, "files", value)
        with pytest.raises((ValueError, KeyError)):
            staging_with_cp(group)
    assert staging_with_cp(document_group()) == [("DOC-KIND", "DOC-KIND-000001", "staging.groups.kind_id")]
    for patch in ({"parent_key": ""}, {"parent_key": None}, {"parent_key": "VEH-000001", "parent_type": "VEH"},
                  {"parent_type": None}, {"document_id": ""}, {"parent_id": ""}, {"expected_version": 1},
                  {"document_id": "DOCUMENT-000001"}, {"document_id": "DOCUMENT-000001", "expected_version": 1},
                  {"revision_id": None}, {"replace_file_id": None}):
        with pytest.raises(ValueError):
            staging_with_cp({**document_group(), **patch})
    for key in ("parent_key", "parent_type", "parent_id", "document_id"):
        group = document_group()
        group.pop(key)
        with pytest.raises(ValueError):
            staging_with_cp(group)


@pytest.mark.parametrize(("parent", "document_exists", "valid"), [
    ("existing-cp", True, True), ("existing-cp", False, True), ("new-cp", False, True), ("new-cp", True, False),
    ("existing-veh", True, True), ("existing-veh", False, True), ("new-veh", False, True), ("new-veh", True, False),
    ("direct-veh", True, True), ("direct-veh", False, True),
])
def test_prepared_document_parent_and_document_states(parent, document_exists, valid):
    group, choices, comparison = document_group(), {}, {}
    if document_exists:
        group.update(document_id="DOCUMENT-000001", expected_version=1)
    if parent.endswith("-cp"):
        cp = cp_row("CP-000001" if parent == "existing-cp" else None)
        comparison["cp"] = cp
        choices.update(cp_target=cp["target"], cp_fields=["name", "type"])
        group["parent_id"] = cp["id"]
    else:
        vehicle = vehicle_row()
        identifier = None if parent == "new-veh" else "VEH-000001"
        vehicle.update(id=identifier, version=1 if identifier else None)
        if parent != "direct-veh":
            comparison["vehicles"] = [vehicle]
            choices["vehicles"] = {"v:5": {"include": parent == "new-veh"}}
        group.update(parent_type="VEH", parent_key=identifier if parent == "direct-veh" else "v:5", parent_id=identifier)
    if not valid:
        with pytest.raises(ValueError):
            refs.staging_group(group, comparison=comparison, choices=choices)
        return
    extracted = refs.staging_group(group, comparison=comparison, choices=choices)
    assert any(kind == "DOCUMENT" for kind, *_ in extracted) is document_exists
    assert any(kind == group["parent_type"] and identifier == group["parent_id"]
               for kind, identifier, _ in extracted) is (group["parent_id"] is not None)


@pytest.mark.parametrize("damage", [
    "missing-row", "duplicate-row", "wrong-kind", "wrong-row", "wrong-parent", "direct-id-mismatch",
    "not-selected", "withdrawal", "missing-withdrawal", "invalid-plate", "invalid-row", "invalid-field", "unresolved-type",
    "pending-version", "stored-existing-veh", "existing-version", "existing-parent-null", "existing-cp-null",
    "missing-cp", "different-cp-choice", "unselected-cp", "cp-error",
    "version-without-document", "document-without-version", "boolean-version", "replacement-new-doc",
    "existing-file-new-doc",
])
def test_prepared_parent_requires_the_same_unambiguous_saved_choice(damage):
    vehicle, group = vehicle_row(), {**document_group(), "parent_type": "VEH", "parent_key": "v:5"}
    comparison, choices = {"vehicles": [vehicle]}, {"vehicles": {"v:5": {"include": True}}}
    if damage == "missing-row":
        comparison["vehicles"] = []
    elif damage == "duplicate-row":
        comparison["vehicles"].append(deepcopy(vehicle))
    elif damage in ("wrong-kind", "wrong-row", "wrong-parent", "direct-id-mismatch"):
        group.update({"wrong-kind": {"parent_type": "CP"}, "wrong-row": {"parent_key": "v:6"},
                      "wrong-parent": {"parent_id": "VEH-000002"},
                      "direct-id-mismatch": {"parent_key": "VEH-000002", "parent_id": "VEH-000001"}}[damage])
    elif damage == "not-selected":
        choices["vehicles"]["v:5"]["include"] = False
    elif damage == "withdrawal":
        vehicle["withdrawal"] = True
    elif damage == "missing-withdrawal":
        vehicle.pop("withdrawal")
    elif damage == "invalid-plate":
        vehicle["plate_number"] = 1
    elif damage == "invalid-row":
        vehicle["errors"] = ["Повтор госномера"]
    elif damage in ("invalid-field", "unresolved-type"):
        vehicle["fields"]["type_id"] = {"error": "Проверьте тип", "selected": damage == "invalid-field"}
    elif damage == "pending-version":
        vehicle["version"] = 1
    elif damage in ("stored-existing-veh", "existing-version", "existing-parent-null"):
        vehicle.update(id="VEH-000001", version=None if damage == "existing-version" else 1)
        if damage == "existing-version":
            group["parent_id"] = vehicle["id"]
        if damage == "existing-parent-null":
            choices["vehicles"]["v:5"]["include"] = False
    elif damage in ("existing-cp-null", "missing-cp", "different-cp-choice", "unselected-cp", "cp-error"):
        cp = cp_row("CP-000001" if damage == "existing-cp-null" else None)
        group = document_group()
        comparison = {} if damage == "missing-cp" else {"cp": cp}
        choices = {"cp_target": "CP-000002" if damage == "different-cp-choice" else cp["target"],
                   "cp_fields": [] if damage == "unselected-cp" else ["name", "type"]}
        if damage == "cp-error":
            cp["error"] = "Существующий контрагент"
    else:
        group.update({
            "version-without-document": {"expected_version": 1},
            "document-without-version": {"document_id": "DOCUMENT-000001"},
            "boolean-version": {"document_id": "DOCUMENT-000001", "expected_version": True},
            "replacement-new-doc": {"replacements": ["FILE-000001"]},
            "existing-file-new-doc": {"files": [file_plan(existing=True)]},
        }[damage])
        vehicle.update(id="VEH-000001", version=1)
        group["parent_id"] = vehicle["id"]
    with pytest.raises((ValueError, KeyError)):
        refs.staging_group(group, comparison=comparison, choices=choices)


def test_vehicle_preview_distinguishes_absence_of_vehicle_unresolved_type_and_new_cp():
    vehicle = vehicle_row()
    owner = vehicle["fields"]["owner"]
    owner["proposed"] = ["CP", "$CP"]
    choices = {"cp_target": "new", "vehicles": {"v:5": {"include": True, "fields": ["type_id"], "type_id": ""}}}
    vehicle["fields"]["type_id"] = {"proposed": "Неизвестное название", "error": "Тип не распознан", "selected": True}
    def extract(**patch):
        return refs.questionnaire_selection(preview_row(**({"cp": cp_row(), "vehicle": vehicle, "choices": choices} | patch)))

    assert extract() == []
    for invalid in ([], {}, True, 123):
        vehicle["fields"]["type_id"]["proposed"] = invalid
        with pytest.raises(ValueError):
            extract()
    vehicle["fields"]["type_id"] = {"proposed": "TYPE-000001", "error": None, "selected": True}
    assert any(r[0] == "TYPE" for r in extract())
    with pytest.raises(ValueError):
        extract(cp=cp_row("CP-000001"), choices={**choices, "cp_target": "CP-000001"})
    with pytest.raises(ValueError):
        extract(cp={**cp_row(), "id": "CP-000001", "version": 1})
    owner["current"] = ["CP", "$CP"]
    with pytest.raises(ValueError):
        extract()
    owner["current"] = None
    for patch in ({"id": ""}, {"id": "VEH-000001"}, {"version": 1}, {"id": "VEH-000001", "version": True}):
        with pytest.raises(ValueError):
            extract(vehicle={**vehicle, **patch})
    vehicle.update(id="VEH-000001", version=1)
    assert any(r[0] == "VEH" for r in extract())
    for field in ("id", "version"):
        damaged = {k: v for k, v in vehicle.items() if k != field}
        with pytest.raises(ValueError):
            extract(vehicle=damaged)
    for bad_type in ("Ошибка", "new", "CP-000001", {}):
        choices["vehicles"]["v:5"]["type_id"] = bad_type
        with pytest.raises(ValueError):
            extract()


def reserved_file_path(path, item):
    if path == "staging":
        return staging_with_cp({**existing_document_group(), "files": [item]}, existing_parent=True)
    if path == "prepared":
        return refs.questionnaire_upload(SimpleNamespace(prepared={str(uuid4()): [item]}))
    if path == "acceptance":
        return refs.file_acceptance(SimpleNamespace(plan={**acceptance_plan(), "files": [{**item, "stage_id": None}]}))
    file = {key: value for key, value in item.items() if key != "existing"} | {"token": "a" * 64}
    if path == "response-stage":
        return refs.approval_attempt(SimpleNamespace(action=path, target_id="APPROVAL-000001",
                                                     plan={"file": file, "session_hash": "synthetic"}))
    return refs.approval_attempt(SimpleNamespace(action="letter", target_id="",
                                                 plan={"files": [file], "snapshots": [approval_vehicle()], "created_at": ""}))


@pytest.mark.parametrize("path", ["staging", "prepared", "acceptance", "response-stage", "letter"])
@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_planned_file_id_is_strict_even_when_reserved_and_unpublished(path, value):
    item = file_plan(existing=False)
    set_or_remove(item, "id", value)
    with pytest.raises((ValueError, KeyError)):
        reserved_file_path(path, item)


@pytest.mark.parametrize("path", ["staging", "prepared", "acceptance"])
def test_only_an_existing_planned_file_becomes_an_active_reference(path):
    assert not any(r[0] == "FILE" for r in reserved_file_path(path, file_plan(existing=False)))
    assert any(r[:2] == ("FILE", "FILE-000001") for r in reserved_file_path(path, file_plan(existing=True)))


@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_correction_object_requires_valid_file_id(value):
    correction = {"file_id": "FILE-000001", "stage_id": None}
    set_or_remove(correction, "file_id", value)
    plan = {**acceptance_plan(), "correction": correction}
    with pytest.raises((ValueError, KeyError)):
        refs.file_acceptance(SimpleNamespace(plan=plan))


def test_file_acceptance_revision_is_nullable_only_for_empty_noop():
    base = acceptance_plan()
    assert refs.file_acceptance(SimpleNamespace(plan=base)) == []
    assert refs.file_acceptance(SimpleNamespace(plan={**base, "correction": None})) == []
    assert refs.file_acceptance(SimpleNamespace(plan={**base, "correction": {"file_id": "FILE-000001", "stage_id": None}}))
    assert refs.file_acceptance(SimpleNamespace(plan={**base, "revision_id": None, "noop": True})) == []
    for patch in ({"revision_id": None}, {"revision_id": "new"}, {"revision_id": "FILE-000001"}, {"revision_id": []},
                  {"revision_id": None, "noop": True, "files": [file_plan()]}, {"noop": 1}, {"files": None}):
        with pytest.raises(ValueError):
            refs.file_acceptance(SimpleNamespace(plan={**base, **patch}))
    for field in ("revision_id", "files", "uploaded"):
        with pytest.raises(ValueError):
            refs.file_acceptance(SimpleNamespace(plan={k: v for k, v in base.items() if k != field}))


@pytest.mark.parametrize("section,kind", [("objects", "CP"), ("objects", "CPREP"), ("objects", "VEH"),
                                           ("objects", "DOCUMENT"), ("changes", "DOCUMENT")])
@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_receipt_references_cannot_disappear_or_fall_back(section, kind, value):
    entry = {"kind": kind, "id": f"{kind}-000001"}
    set_or_remove(entry, "id", value)
    if kind == "DOCUMENT" and (section == "changes" or value is not MISSING):
        entry["document_id"] = "DOCUMENT-000001"
    result = {"objects": [], "changes": [], "cp_id": None, section: [entry]}
    with pytest.raises((ValueError, KeyError)):
        refs.questionnaire_outcome(SimpleNamespace(result=result))


def test_document_receipt_supports_actual_variants_and_requires_collections():
    result = {"objects": [{"kind": "DOCUMENT", "document_id": "DOCUMENT-000001"},
                          {"kind": "DOCUMENT", "id": "DOCUMENT-000002"},
                          {"kind": "DOCUMENT", "id": "DOCUMENT-000003", "document_id": "DOCUMENT-000003"}],
              "changes": [{"kind": "DOCUMENT", "id": "DOCUMENT-000001"}], "cp_id": None}
    assert {entry[1] for entry in refs.questionnaire_outcome(SimpleNamespace(result=result))} == {
        "DOCUMENT-000001", "DOCUMENT-000002", "DOCUMENT-000003",
    }
    for wrong in ({"kind": "DOCUMENT", "id": "DOCUMENT-000001", "document_id": "DOCUMENT-000002"},
                  {"kind": "DOCUMENT", "id": "DOCUMENT-000001", "document_id": None},
                  {"kind": "TYPE", "id": "TYPE-000001"}, {"id": "CP-000001"}):
        with pytest.raises((ValueError, KeyError)):
            refs.questionnaire_outcome(SimpleNamespace(result={**result, "objects": [wrong]}))
    for field in ("objects", "changes", "cp_id"):
        damaged = {k: v for k, v in result.items() if k != field}
        with pytest.raises(KeyError):
            refs.questionnaire_outcome(SimpleNamespace(result=damaged))
        with pytest.raises(ValueError):
            refs.questionnaire_outcome(SimpleNamespace(result={**result, field: ""}))
    for section in ("objects", "changes"):
        with pytest.raises(ValueError):
            refs.questionnaire_outcome(SimpleNamespace(result={**result, section: None}))


@pytest.mark.parametrize("source,key", [("snapshot", "vehicle_id"), ("snapshot", "document_id"),
                                         ("snapshot", "revision_id"), ("calibration", "vehicle_id"),
                                         ("letter", "request_id"), ("response", "request_id")])
@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_approval_snapshot_and_outcome_ids_are_required(source, key, value):
    if source == "snapshot":
        vehicle = approval_vehicle()
        set_or_remove(vehicle, key, value)
        with pytest.raises((ValueError, KeyError)):
            refs.snapshot_references([vehicle], "vehicles_snapshot")
    else:
        result = {} if value is MISSING else {key: value}
        row = SimpleNamespace(action=source, target_id="" if source == "letter" else "APPROVAL-000001", result=result)
        with pytest.raises((ValueError, KeyError)):
            refs.approval_outcome(row)


@pytest.mark.parametrize("action,kind", [("calibration", "VEH"), ("response", "APPROVAL"), ("response-stage", "APPROVAL")])
@pytest.mark.parametrize("value", [pytest.param(MISSING, id="missing"), *INVALID_ID])
def test_approval_pending_target_ids_are_strict(action, kind, value):
    file = {key: val for key, val in file_plan().items() if key != "existing"} | {"token": "a" * 64}
    plan = {"file": file, "session_hash": "synthetic"} if action == "response-stage" else {}
    row = {"action": action, "target_id": f"{kind}-000001", "plan": plan, "result": {"consumed_by": str(uuid4())}}
    assert refs.approval_attempt(SimpleNamespace(**row))[0][:2] == (kind, f"{kind}-000001")
    set_or_remove(row, "target_id", value)
    with pytest.raises((ValueError, AttributeError)):
        refs.approval_attempt(SimpleNamespace(**row))
    if action == "response-stage":
        with pytest.raises((ValueError, AttributeError)):
            refs.approval_outcome(SimpleNamespace(**row))


def test_only_letter_accepts_empty_target_and_job_ids_are_service_keys():
    data = SimpleNamespace(action="letter", target_id="", result={"request_id": "APPROVAL-000001"},
                           plan={"files": [], "snapshots": [approval_vehicle()], "created_at": ""})
    assert refs.approval_attempt(data)[0][:2] == ("VEH", "VEH-000001")
    assert refs.approval_outcome(data)[0][:2] == ("APPROVAL", "APPROVAL-000001")
    for wrong in (None, "APPROVAL-000001", "new", True):
        data.target_id = wrong
        with pytest.raises(ValueError):
            refs.approval_attempt(data)
        with pytest.raises(ValueError):
            refs.approval_outcome(data)
    job = SimpleNamespace(resource_id=str(uuid4()), payload={"command_id": str(uuid4()), "revision": 1,
                          "preview_id": "preview", "without_documents": False, "session_hash": None})
    assert refs.questionnaire_job(job) == ()
    job.payload["command_id"] = "VEH-000001"
    with pytest.raises(ValueError):
        refs.questionnaire_job(job)


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


def deletion_storage_snapshot(db, disk):
    tables = (
        "arm2_approval_requests", "arm2_approval_decisions", "arm2_approval_attempts",
        "arm2_bulk_sessions", "arm2_bulk_receipts", "arm2_vehicle_types", "arm2_document_kinds",
        "arm2_organizations", "arm2_counterparties", "arm2_vehicles", "arm2_documents",
        "arm2_document_files", "arm2_document_file_stages", "arm2_document_revisions",
        "arm2_document_revision_files", "arm2_document_revision_contexts", "arm2_document_file_acceptances",
        "arm2_questionnaire_drafts", "arm2_questionnaire_receipts", "arm2_questionnaire_uploads",
        "arm2_deletion_marks", "arm2_deletion_batches", "arm2_history_events", "arm2_background_operations",
    )
    with db.get_bind().connect() as conn:
        rows = {table: conn.exec_driver_sql(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY 1").scalars().all()
                for table in tables}
    files = [{str(file.relative_to(root)): sha256(file.read_bytes()).hexdigest()
              for file in root.rglob("*") if file.is_file()} for root in (disk.path, file_storage.zone())]
    return rows, files


def assert_registry_refuses(c, db, disk, keys, checked, path):
    before = deletion_storage_snapshot(db, disk)
    body = {"objects": [{"object_type": kind, "object_id": identifier} for kind, identifier in keys]}
    for response in (c.post(BASE + "/preview", json=body), execute(c, checked["id"])):
        assert response.status_code == 409 and path in response.json()["detail"], response.text
    assert ok(c.get(f"{BASE}/batches/{checked['id']}"))["state"] == "CHECKED"
    assert deletion_storage_snapshot(db, disk) == before
    assert db.get(DeletionBatch, checked["id"]).executed_at is None
    assert db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "DELETED")) is None
    assert not queue_snapshot(db)
    db.rollback()


def test_null_response_file_id_stops_preview_and_stale_execute_without_losing_approval(qa):
    c, db, _, _, _, disk = qa
    _, response = letter(c, prepare(qa)[:1])
    identifier = ok(response, 201)["request_id"]
    path = f"/api/arm2/approvals/{identifier}"
    detail = ok(c.get(path))
    keys = [("APPROVAL", identifier), ("APPROVAL-ITEM", detail["decisions"][0]["id"])]
    for key in keys:
        mark(c, *key)
    batch = preview(c, *keys)
    assert batch["can_execute"] and detail["response_file_ids"] == []
    request = db.get(ApprovalRequest, identifier)
    request.response_file_ids = [None]
    db.commit()
    assert_registry_refuses(c, db, disk, keys, batch, "vehicles_snapshot/response_file_ids")
    assert db.get(ApprovalRequest, identifier, populate_existing=True).response_file_ids == [None]
    original = f"{path}/files/{detail['letter_file_id']}/download"
    download = c.get(original)
    assert download.status_code == 200 and download.content.startswith(b"%PDF-")
    # Restore only the deliberate test fixture mutation before reopening the valid source document.
    request.response_file_ids = []
    db.commit()
    assert ok(c.get(path)) == detail


def unicode_id(identifier):
    return identifier.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


@pytest.mark.parametrize("kind", ["TYPE", "CP"])
def test_unicode_bulk_id_stops_preview_and_stale_execute_before_filtering(qa, kind):
    c, db, _, parents, _, disk = qa
    typ = ok(c.post("/api/arm2/types", json={
        "name": "Синтетический тип для B4", "plate_category": "Автомобильный", "requires_sts": True,
        "requires_calibration": False, "requires_wialon": False,
    }), 201)["id"]
    cp = parents["cp"]
    draft = upload(c, f"а\tб\tв\tг\nА813ВС86\t{typ}\tCP\t{cp}")
    draft = edit(c, draft, mapping=["plate_number", "type_id", "owner_kind", "owner_id"])
    key = (kind, typ if kind == "TYPE" else cp)
    mark(c, *key)
    checked = preview(c, key)
    assert checked["can_execute"]
    field = "type_id" if kind == "TYPE" else "owner"
    draft = edit(c, draft, decisions={draft["plans"][0]["key"]: {field: 0}})
    assert not preview(c, key)["can_execute"]
    row = db.get(BulkSession, draft["id"], populate_existing=True)
    original = deepcopy(row.payload)
    malformed = deepcopy(original)
    option = malformed["plans"][0]["fields"][field][0]
    assert option["value"] == (typ if kind == "TYPE" else ["CP", cp])
    option["value"] = unicode_id(typ) if kind == "TYPE" else ["CP", unicode_id(cp)]
    row.payload = malformed
    db.commit()
    assert_registry_refuses(c, db, disk, [key], checked, "arm2_bulk_sessions.payload.plans")
    row.payload = original
    db.commit()
    assert not preview(c, key)["can_execute"]
    assert reopen(c, draft)["decisions"] == draft["decisions"]


@pytest.mark.parametrize("field", ["file_ids", "revision_id"])
def test_unicode_file_or_revision_stops_preview_and_stale_execute(qa, field):
    c, db, _, _, _, disk = qa
    _, response = letter(c, prepare(qa)[:1])
    identifier = ok(response, 201)["request_id"]
    path = f"/api/arm2/approvals/{identifier}"
    detail = ok(c.get(path))
    keys = [("APPROVAL", identifier), ("APPROVAL-ITEM", detail["decisions"][0]["id"])]
    for key in keys:
        mark(c, *key)
    checked = preview(c, *keys)
    assert checked["can_execute"]
    request = db.get(ApprovalRequest, identifier)
    original = deepcopy(request.vehicles_snapshot)
    malformed = deepcopy(original)
    target = malformed[0][field]
    malformed[0][field] = [unicode_id(target[0]), *target[1:]] if field == "file_ids" else unicode_id(target)
    request.vehicles_snapshot = malformed
    db.commit()
    assert_registry_refuses(c, db, disk, keys, checked, "vehicles_snapshot/response_file_ids")
    request.vehicles_snapshot = original
    db.commit()
    assert ok(c.get(path)) == detail and preview(c, *keys)["can_execute"]


@pytest.mark.parametrize("damage", ["current_files", "replacements"])
def test_invalid_document_file_paths_stop_deletion_for_real_b5_draft(b5, damage):
    c, db, _, parents, _, disk = b5
    kind = doc_kind(c)
    url = f"/api/arm2/veh/{parents['veh']}/documents"
    identifier = ok(c.post(url, json={"kind_ref": kind, "name": "СТС"}), 201)["id"]
    url += "/" + identifier
    original = pdf("original prior to B5 replacement")
    ok(accept(c, url, 1, [stage(c, url, original)]))
    key = ("ORG", parents["org"])
    mark(c, *key)
    checked = preview(c, key)
    assert checked["can_execute"]
    form = workbook(rows=[{"E": "А123ВС86", "P": "new.pdf"}])
    draft = upload_questionnaire(c, transport(form, {"new.pdf": pdf("new")}), True)
    draft = choose_questionnaire(c, draft, cp_target=parents["cp"], documents={"v:5:sts": {
        "kind_id": kind, "document_id": identifier, "accept": True,
    }})
    if damage == "replacements":
        disk.fail_at = len(disk.uploads)
        apply_questionnaire(c, draft, status=503)
        disk.fail_at = None
    db.rollback()
    row = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
    extracted = refs.questionnaire_selection(row)
    assert any(item[0] == "FILE" for item in extracted)
    if damage == "current_files":
        stored = deepcopy(row.preview)
        assert stored["documents"][0]["current_files"][0]["id"]
        stored["documents"][0]["current_files"][0]["id"] = None
        row.preview = stored
    else:
        stored = deepcopy(row.staging)
        assert stored["groups"][0]["replacements"]
        stored["groups"][0]["replacements"] = [None]
        row.staging = stored
    db.commit()
    assert_registry_refuses(c, db, disk, [key], checked, "choices/preview/staging")
    revision = bundle(c, url)["revisions"][0]
    file_id = revision["files"][0]["id"]
    assert c.get(f"{url}/revisions/{revision['id']}/files/{file_id}/download").content == original


@pytest.mark.parametrize(("document_exists", "damage"), [
    (True, {"parent_id": None}), (False, {"parent_id": None}), (False, {"parent_key": "VEH-999999"}),
])
def test_inconsistent_prepared_b5_parent_stops_preview_and_stale_execute(b5, document_exists, damage):
    c, db, _, parents, _, disk = b5
    kind = doc_kind(c)
    url = f"/api/arm2/veh/{parents['veh']}/documents"
    identifier = None
    if document_exists:
        identifier = ok(c.post(url, json={"kind_ref": kind, "name": "СТС"}), 201)["id"]
        original_file = pdf("original prior to B5 replacement")
        ok(accept(c, f"{url}/{identifier}", 1, [stage(c, f"{url}/{identifier}", original_file)]))
    key, parent_key = ("ORG", parents["org"]), ("VEH", parents["veh"])
    for identity in (key, parent_key):
        mark(c, *identity)
    checked = preview(c, key)
    assert checked["can_execute"]
    form = workbook(rows=[{"E": "А123ВС86", "P": "new.pdf"}])
    draft = upload_questionnaire(c, transport(form, {"new.pdf": pdf("new")}), True)
    draft = choose_questionnaire(c, draft, cp_target=parents["cp"], documents={"v:5:sts": {
        "kind_id": kind, "document_id": identifier or "new", "accept": True,
    }})
    disk.fail_at = len(disk.uploads)
    apply_questionnaire(c, draft, status=503)
    disk.fail_at = None
    row = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
    valid_staging = deepcopy(row.staging)
    group = valid_staging["groups"][0]
    assert group["parent_id"] == parents["veh"] and group["parent_key"] == "v:5"
    assert group["document_id"] == identifier
    assert (group["expected_version"] is not None) is document_exists
    assert not preview(c, parent_key)["can_execute"]
    malformed = deepcopy(valid_staging)
    malformed["groups"][0].update(damage)
    row.staging = malformed
    db.commit()
    assert_registry_refuses(c, db, disk, [key], checked, "choices/preview/staging")
    row.staging = valid_staging
    db.commit()
    assert reopen_questionnaire(c, draft)["choices"] == draft["choices"]
    assert not preview(c, parent_key)["can_execute"]
    assert ok(c.delete(f"/api/arm2/questionnaires/{draft['id']}"))["status"] == "CANCELLED"
    assert preview(c, key)["can_execute"]


def test_required_corrected_file_ref_blocks_deletion_via_the_common_registry(qa):
    c, db, _, parents, _, disk = qa
    url = create(qa)
    original = pdf("original prior to correction")
    ok(accept(c, url, 1, [stage(c, url, original)]))
    revision = bundle(c, url)["revisions"][0]
    file_id = revision["files"][0]["id"]
    key = ("ORG", parents["org"])
    mark(c, *key)
    checked = preview(c, key)
    correction = str(uuid4())
    candidate = stage(c, url, pdf("a valid replacement"))["stage_id"]
    disk.fail_at = len(disk.uploads)
    assert correct(c, url, 2, file_id, candidate, key=correction).status_code == 503
    disk.fail_at = None
    row = db.get(FileAcceptance, correction)
    assert ("FILE", file_id, "plan.correction.file_id") in refs.file_acceptance(row)
    payload = {col.key: getattr(row, col.key) for col in FileAcceptance.__table__.columns if col.key != "id"}
    plan = deepcopy(row.plan)
    plan["correction"]["file_id"] = None
    db.add(FileAcceptance(**{**payload, "id": str(uuid4()), "plan": plan}))
    db.commit()
    assert_registry_refuses(c, db, disk, [key], checked, "plan.files/plan.correction")
    assert c.get(f"{url}/revisions/{revision['id']}/files/{file_id}/download").content == original


@pytest.mark.parametrize("section", ["objects", "changes"])
def test_missing_receipt_entry_id_stops_deletion_and_preserves_original_protocol(b5, section):
    c, db, _, parents, _, disk = b5
    key = ("ORG", parents["org"])
    mark(c, *key)
    checked = preview(c, key)
    draft = choose_questionnaire(c, upload_questionnaire(c, workbook({"B18": "+7 (901) 222-33-44"})),
                                 cp_target=parents["cp"], cp_fields=["phone"])
    result, command = apply_questionnaire(c, draft)
    row = db.get(QuestionnaireReceipt, command["command_id"])
    assert refs.questionnaire_outcome(row)
    malformed = deepcopy(row.result)
    malformed[section][0].pop("id")
    bad_command = str(uuid4())
    malformed["command_id"] = malformed["correlation_id"] = bad_command
    db.add(QuestionnaireReceipt(command_id=bad_command, draft_id=row.draft_id, author_id=row.author_id,
                                fingerprint="a" * 64, result=malformed))
    db.commit()
    assert_registry_refuses(c, db, disk, [key], checked, "result.objects/changes/cp_id")
    replay, _ = apply_questionnaire(c, draft, body=command)
    assert replay == result


def test_real_b3_empty_noop_can_reopen_without_revision(qa):
    c, db, _, _, _, disk = qa
    url = create(qa)
    command = str(uuid4())
    outcome = ok(accept(c, url, 1, [], command))
    receipt = db.get(FileAcceptance, command)
    assert receipt.plan["noop"] is True and receipt.plan["revision_id"] is None
    assert refs.file_acceptance(receipt) == [] and disk.uploads == []
    assert outcome["no_op"] and bundle(c, url)["current_revision_id"] is None
    assert ok(accept(c, url, 1, [], command)) == outcome


@pytest.mark.parametrize("partial", [False, True])
def test_real_b5_new_parent_and_document_with_unresolved_type_elsewhere(b5, partial):
    c, db, _, _, _, disk = b5
    kind = doc_kind(c)
    company_kind = doc_kind(c, "Карточка предприятия", "CP")
    original = pdf("new B5 original with no published IDs")
    company_file = pdf("new company original with no published IDs")
    form = workbook({"B5": "ООО", "B12": "Синтетический новый контрагент", "B28": "company.pdf"}, [
        {"I": "А812ВС86", "J": "Синтетическая новая машина", "P": "sts.pdf"},
        {"I": "А814ВС86", "H": "Неизвестный исходный тип"},
    ])
    draft = choose_questionnaire(c, upload_questionnaire(
        c, transport(form, {"sts.pdf": original, "company.pdf": company_file}), True),
                                 cp_target="new", cp_fields=["type", "name"],
                                 vehicles={"v:5": {"include": True, "fields": ["name", "owner"]},
                                           "v:6": {"include": False, "fields": []}},
                                 documents={"company": {"kind_id": company_kind, "accept": True},
                                            "v:5:sts": {"kind_id": kind, "accept": True}})
    row = db.get(QuestionnaireDraft, draft["id"])
    proposal = row.preview["vehicles"][1]["fields"]["type_id"]
    assert proposal["error"] and proposal["proposed"] == "Неизвестный исходный тип"
    assert set(refs.questionnaire_selection(row)) == {
        ("DOC-KIND", identifier, path) for identifier in (kind, company_kind)
        for path in ("choices.documents.kind_id", "preview.documents.kind_id")
    }
    db.rollback()
    disk.fail_at = 0
    _, command = apply_questionnaire(c, draft, status=503)
    disk.fail_at = None
    pending = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
    assert {group["parent_type"] for group in pending.staging["groups"]} == {"CP", "VEH"}
    for prepared in pending.staging["groups"]:
        assert prepared["parent_id"] is None and prepared["document_id"] is None and prepared["expected_version"] is None
        assert refs.staging_group(prepared, comparison=pending.preview, choices=pending.choices) == [
            ("DOC-KIND", prepared["kind_id"], "staging.groups.kind_id")]
    upload_rows = list(db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft["id"])))
    assert len(upload_rows) == 2 and all(row.prepared and refs.questionnaire_upload(row) == [] for row in upload_rows)
    db.rollback()
    if partial:
        saved, _ = apply_questionnaire(c, draft, without_documents=True)
        assert {entry["kind"] for entry in saved["objects"]} == {"CP", "VEH"}
        pending = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
        assert pending.staging == {} and pending.choices["cp_target"] == saved["cp_id"]
        assert pending.preview["vehicles"][0]["id"] and pending.preview["vehicles"][0]["version"]
        assert all(row.prepared for row in upload_rows)
        db.rollback()
        draft = choose_questionnaire(c, reopen_questionnaire(c, draft), documents=draft["choices"]["documents"])
        disk.fail_at = 0
        _, command = apply_questionnaire(c, draft, status=503)
        disk.fail_at = None
        pending = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
        assert all(group["parent_id"] is not None for group in pending.staging["groups"])
        assert {"CP", "VEH"} <= {ref[0] for ref in refs.questionnaire_selection(pending)}
        group = next(group for group in pending.staging["groups"] if group["parent_type"] == "VEH")
        inconsistent = deepcopy(pending.preview)
        inconsistent["vehicles"][0].update(id=None, version=None)
        choices = {**pending.choices, "vehicles": {"v:5": {"include": True}}}
        with pytest.raises(ValueError, match="staging.groups"):
            refs.staging_group({**group, "parent_id": None}, comparison=inconsistent, choices=choices,
                               payload=pending.payload)
        db.rollback()
    result, _ = apply_questionnaire(c, draft, body=command)
    receipt = db.get(QuestionnaireReceipt, command["command_id"])
    expected = {"DOCUMENT"} if partial else {"CP", "VEH", "DOCUMENT"}
    assert {ref[0] for ref in refs.questionnaire_outcome(receipt)} == expected | {"CP"}
    assert {entry["kind"] for entry in result["objects"]} == expected
    replay, _ = apply_questionnaire(c, draft, body=command)
    assert replay == result
    closed = reopen_questionnaire(c, draft)
    assert closed["status"] == "ACCEPTED" and closed["receipt"] == result
    downloaded = set()
    for entry in result["objects"]:
        if entry["kind"] != "DOCUMENT":
            continue
        url = "/api" + entry["url"]
        revision = bundle(c, url)["revisions"][0]
        file_id = revision["files"][0]["id"]
        original_response = c.get(f"{url}/revisions/{revision['id']}/files/{file_id}/download")
        assert original_response.status_code == 200
        downloaded.add(original_response.content)
    assert downloaded == {original, company_file}


def test_real_b5_direct_vehicle_binding_needs_no_workbook_row(b5):
    c, db, _, parents, _, disk = b5
    kind = doc_kind(c)
    content = pdf("direct vehicle binding")
    draft = upload_questionnaire(c, transport(workbook(), {"direct.pdf": content}), True)
    attachment = draft["comparison"]["documents"][0]["key"]
    assert attachment.startswith("unbound:")
    draft = choose_questionnaire(c, draft, cp_target=parents["cp"], documents={attachment: {
        "parent_key": parents["veh"], "kind_id": kind, "accept": True,
    }})
    disk.fail_at = 0
    _, command = apply_questionnaire(c, draft, status=503)
    disk.fail_at = None
    pending = db.get(QuestionnaireDraft, draft["id"], populate_existing=True)
    assert pending.preview["vehicles"] == []
    group, = pending.staging["groups"]
    assert group["parent_id"] == group["parent_key"] == parents["veh"]
    assert ("VEH", parents["veh"], "staging.groups.parent_id") in refs.questionnaire_selection(pending)
    db.rollback()
    result, _ = apply_questionnaire(c, draft, body=command)
    assert {entry["kind"] for entry in result["objects"]} == {"DOCUMENT"}
    assert reopen_questionnaire(c, draft)["receipt"] == result
    assert disk.location(disk.uploads[0]).read_bytes() == content
