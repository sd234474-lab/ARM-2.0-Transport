"""Synthetic end-to-end approval evidence; no live cloud, credentials or owner data."""

import io
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, pdf, png, stage
from test_arm2_document_file_api import qa as qa
from test_arm2_questionnaire_workflow import b5 as b5

from app.arm2.approval_models import ApprovalDecision, ApprovalRequest
from app.arm2.document_file_models import DocumentFile
from app.arm2.history_models import HistoryEvent


def ok(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def prepare(qa):
    c, db, user, parents, kind, fake = qa
    type_id = ok(
        c.post(
            "/api/arm2/types",
            json={
                "name": "Транспорт тест",
                "plate_category": "Автомобильный",
                "requires_sts": True,
                "requires_calibration": True,
                "requires_wialon": False,
            },
        ),
        201,
    )["id"]
    calibration_kind = ok(
        c.post(
            "/api/arm2/document-kinds",
            json={"name": "Тарировка", "applicable_object_types": ["VEH"], "max_current_documents": None},
        ),
        201,
    )["id"]
    vehicles = []
    for i, plate in enumerate(["E679PH186", "A123BC186"]):
        vehicle = ok(
            c.post(
                "/api/arm2/vehicles", json={"name": f"679HOWO {i}", "plate_number": plate, "type_id": type_id}
            ),
            201,
        )
        path = f"/api/arm2/veh/{vehicle['id']}/documents"
        document = ok(c.post(path, json={"name": "Тарировка", "kind_ref": calibration_kind}), 201)
        path += "/" + document["id"]
        ok(accept(c, path, document["version"], [stage(c, path, pdf(str(i)))]))
        document = ok(c.get(path))
        command = {
            "command_id": str(uuid.uuid4()),
            "vehicle_version": vehicle["version"],
            "document_id": document["id"],
            "document_version": document["version"],
            "revision_id": bundle(c, path)["current_revision_id"],
            "plate_number": vehicle["plate_number"],
            "volume_m3": 25.0,
        }
        confirmed = ok(c.post(f"/api/arm2/veh/{vehicle['id']}/calibration/confirm", json=command))
        vehicles.append((vehicle["id"], path, command, confirmed))
    return vehicles


def letter(c, vehicles, **extra):
    body = {
        "command_id": str(uuid.uuid4()),
        "contract_number_1": "К-001",
        "contract_number_2": "К-002",
        "vehicles": [
            {"vehicle_id": item[0], "vehicle_version": ok(c.get("/api/arm2/vehicles/" + item[0]))["version"]}
            for item in vehicles
        ],
        **extra,
    }
    return body, c.post("/api/arm2/approvals/letter", json=body)


def test_one_letter_two_numbers_response_roundtrip_and_idempotency(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    body, response = letter(c, vehicles)
    result = ok(response, 201)
    request_id = result["request_id"]
    assert ok(c.post("/api/arm2/approvals/letter", json=body), 201) == result
    request = ok(c.get("/api/arm2/approvals/" + request_id))
    assert {file["id"] for file in request["files"]} == {
        request["letter_file_id"],
        request["package_file_id"],
    }
    assert all(not {"storage_ref", "token", "process_id"} & set(file) for file in request["files"])
    assert [request["contract_number_1"], request["contract_number_2"]] == ["К-001", "К-002"]
    assert len(request["decisions"]) == 2
    assert all(d["state"] == "PENDING" for d in request["decisions"])
    package = c.get(f"/api/arm2/approvals/{request_id}/files/{request['package_file_id']}/download")
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert len(archive.namelist()) == 4  # letter, two originals, traceable manifest
        assert "manifest.json" in archive.namelist()
        generated = archive.read(next(n for n in archive.namelist() if "Согласование" in n))
        assert generated.startswith(b"%PDF-") and b"%%EOF" in generated[-1024:]
    response_file = png()
    stage_id = str(uuid.uuid4())
    endpoint = f"/api/arm2/approvals/{request_id}"
    uploaded = ok(c.put(endpoint + f"/response-stages/{stage_id}?name=response.png", content=response_file))
    assert uploaded["stage_id"] == stage_id
    assert uploaded["stored_name"].endswith(".png") and "-FILE-" in uploaded["stored_name"]
    preview = c.get(endpoint + f"/response-stages/{stage_id}/preview")
    assert preview.content == response_file
    command = {
        "command_id": str(uuid.uuid4()),
        "expected_version": request["version"],
        "stage_id": stage_id,
        "decisions": [
            {
                "vehicle_id": d["vehicle_id"],
                "expected_version": d["version"],
                "state": ["APPROVED", "REJECTED"][i],
            }
            for i, d in enumerate(request["decisions"])
        ],
    }
    saved = ok(c.post(endpoint + "/response", json=command))
    assert ok(c.post(endpoint + "/response", json=command)) == saved
    request = ok(c.get(endpoint))
    assert [d["state"] for d in request["decisions"]] == ["APPROVED", "REJECTED"]
    assert len(request["response_file_ids"]) == 1
    original = c.get(endpoint + f"/files/{request['response_file_ids'][0]}/download")
    assert original.content == response_file
    assert "no-store" in original.headers["cache-control"]
    assert len(ok(c.get("/api/arm2/history/APPROVAL/" + request_id))) == 2
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
    assert db.scalar(select(func.count()).select_from(ApprovalDecision)) == 2
    assert (
        db.scalar(select(func.count()).select_from(DocumentFile).where(DocumentFile.process_id == request_id))
        == 3
    )


def test_confirmation_readonly_noop_stale_and_two_documents(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    vehicle_id, path, command, confirmed = vehicles[0]
    assert confirmed["volume_m3"] == 25
    assert ok(c.post(f"/api/arm2/veh/{vehicle_id}/calibration/confirm", json=command)) == confirmed
    record = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    assert record["calibration_m3"] == 25
    assert record["calibration_confirmed_at"] and record["calibration_confirmed_by"] == user.id
    updated = {**command, "command_id": str(uuid.uuid4()), "vehicle_version": record["version"]}
    noop = ok(c.post(f"/api/arm2/veh/{vehicle_id}/calibration/confirm", json=updated))
    assert noop["no_op"] and not noop["event_ids"]
    assert (
        c.post(
            f"/api/arm2/veh/{vehicle_id}/calibration/confirm",
            json={**updated, "command_id": str(uuid.uuid4()), "vehicle_version": 1},
        ).status_code
        == 409
    )
    # Ordinary writes cannot smuggle proof, and an ordinary partial update preserves it.
    assert (
        c.put(
            "/api/arm2/vehicles/" + vehicle_id,
            json={"expected_version": record["version"], "calibration_m3": 27.0},
        ).status_code
        == 422
    )
    changed = ok(
        c.put(
            "/api/arm2/vehicles/" + vehicle_id,
            json={
                **{
                    key: record[key]
                    for key in ["name", "plate_number", "type_id", "accounting_state", "wialon_unit_id"]
                },
                "expected_version": record["version"],
                "model": "updated",
            },
        )
    )
    assert (
        changed["calibration_m3"] == 25
        and changed["calibration_confirmed_at"] == record["calibration_confirmed_at"]
    )
    # Another unconfirmed calibration must not win find(first).
    document = ok(c.get(path))
    extra = ok(
        c.post(path.rsplit("/", 1)[0], json={"name": "Старая справка", "kind_ref": document["kind_ref"]}), 201
    )
    assert extra["id"] != document["id"]
    ready = ok(c.get(f"/api/arm2/veh/{vehicle_id}/calibration"))
    assert ready["letter_eligible"] and ready["document_id"] == document["id"]
    assert ready["work_readiness_complete"] is False
    listing = path.rsplit("/", 1)[0] + "?include_card_details=true"
    rows = {row["id"]: row for row in ok(c.get(listing))}
    confirmed_row = rows[document["id"]]
    assert confirmed_row["calibration_m3"] == 25
    assert confirmed_row["current_revision_id"] == command["revision_id"]
    assert [file["id"] for file in confirmed_row["preview_files"]] == ready["file_ids"]
    assert all(set(file) == {"id", "original_upload_name"} for file in confirmed_row["preview_files"])
    assert rows[extra["id"]]["calibration_m3"] is None and not rows[extra["id"]]["preview_files"]
    # Missing source bytes blocks the same card/candidate/letter algorithm.
    original = db.get(DocumentFile, ready["file_ids"][0])
    fake.location(original.storage_ref).unlink()
    assert not ok(c.get(f"/api/arm2/veh/{vehicle_id}/calibration"))["letter_eligible"]
    _, response = letter(c, vehicles)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    ok(accept(c, path, document["version"], [stage(c, path, pdf("replacement"))]))
    replaced = next(row for row in ok(c.get(listing)) if row["id"] == document["id"])
    assert replaced["current_revision_id"] != command["revision_id"]
    assert replaced["calibration_m3"] is None
    assert [file["id"] for file in replaced["preview_files"]] != ready["file_ids"]


def test_missing_header_storage_failure_atomic_and_retry(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    _, response = letter(c, vehicles, contract_number_2=" ")
    assert response.status_code == 422
    fake.fail_at = len(fake.uploads) + 1
    body, response = letter(c, vehicles)
    assert response.status_code == 503
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    assert db.scalar(select(func.count()).select_from(ApprovalDecision)) == 0
    assert not db.scalars(select(HistoryEvent).where(HistoryEvent.object_type == "APPROVAL")).all()
    fake.fail_at = None
    result = ok(c.post("/api/arm2/approvals/letter", json=body), 201)
    assert result["request_id"]
    assert len(fake.uploads) == 4  # two source PDFs + letter + package, no duplicate letter upload


@pytest.mark.parametrize(
    "data",
    [b"PK\x03\x04nested zip", b"not a PDF", b"x" * (2 * 1024 * 1024 + 1)],
    ids=["nested-zip", "invalid-pdf", "over-2MiB"],
)
def test_response_rejects_unsupported_and_oversize(qa, data):
    c, *_ = qa
    vehicles = prepare(qa)
    _, response = letter(c, vehicles)
    request_id = ok(response, 201)["request_id"]
    response = c.put(
        f"/api/arm2/approvals/{request_id}/response-stages/{uuid.uuid4()}?name=test.pdf", content=data
    )
    assert response.status_code in {413, 422}


def test_customer_evidence_survives_new_pdf_but_not_changed_characteristics(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    _, response = letter(c, vehicles)
    request_id = ok(response, 201)["request_id"]
    path = "/api/arm2/approvals/" + request_id
    request = ok(c.get(path))
    stage_id = str(uuid.uuid4())
    ok(c.put(path + f"/response-stages/{stage_id}?name=answer.pdf", content=pdf("answer")))
    decision = request["decisions"][0]
    command = {
        "command_id": str(uuid.uuid4()),
        "expected_version": request["version"],
        "stage_id": stage_id,
        "decisions": [
            {
                "vehicle_id": decision["vehicle_id"],
                "expected_version": decision["version"],
                "state": "APPROVED",
            }
        ],
    }
    ok(c.post(path + "/response", json=command))
    vehicle_id, document_path, confirmation, _ = next(v for v in vehicles if v[0] == decision["vehicle_id"])
    ready_path = f"/api/arm2/veh/{vehicle_id}/calibration"
    before = ok(c.get(ready_path))
    assert before["customer"]["state"] == "APPROVED"
    document = ok(c.get(document_path))
    ok(accept(c, document_path, document["version"], [stage(c, document_path, pdf("new PDF same volume"))]))
    after = ok(c.get(ready_path))
    assert after["customer"]["state"] == "APPROVED" and not after["letter_eligible"]
    document = ok(c.get(document_path))
    confirmed = {
        **confirmation,
        "command_id": str(uuid.uuid4()),
        "vehicle_version": after["vehicle_version"],
        "document_version": document["version"],
        "revision_id": bundle(c, document_path)["current_revision_id"],
    }
    ok(c.post(ready_path + "/confirm", json=confirmed))
    assert ok(c.get(ready_path))["customer"]["state"] == "APPROVED"
    ready = ok(c.get(ready_path))
    ok(
        c.post(
            ready_path + "/confirm",
            json={
                **confirmed,
                "command_id": str(uuid.uuid4()),
                "vehicle_version": ready["vehicle_version"],
                "volume_m3": 27.0,
            },
        )
    )
    assert ok(c.get(ready_path))["customer"]["state"] is None
    record = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    changed = ok(
        c.put(
            "/api/arm2/vehicles/" + vehicle_id,
            json={
                **{
                    key: record[key]
                    for key in [
                        "name",
                        "plate_number",
                        "type_id",
                        "accounting_state",
                        "model",
                        "wialon_unit_id",
                    ]
                },
                "plate_number": "А777АА186",
                "expected_version": record["version"],
            },
        )
    )
    assert changed["calibration_m3"] == 27  # Historical proof retained, current eligibility invalidated.
    assert not ok(c.get(ready_path))["letter_eligible"]
    assert len(bundle(c, document_path)["revisions"]) == 2


def test_permissions_response_failure_and_atomic_unknown_vehicle(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    _, response = letter(c, vehicles)
    request_id = ok(response, 201)["request_id"]
    path = "/api/arm2/approvals/" + request_id
    request = ok(c.get(path))
    stage_id = str(uuid.uuid4())
    ok(c.put(path + f"/response-stages/{stage_id}?name=answer.pdf", content=pdf("answer")))
    choices = [
        {"vehicle_id": row["vehicle_id"], "expected_version": row["version"], "state": "APPROVED"}
        for row in request["decisions"]
    ]
    command = {
        "command_id": str(uuid.uuid4()),
        "expected_version": request["version"],
        "stage_id": stage_id,
        "decisions": choices,
    }
    fake.fail_at = len(fake.uploads)
    assert c.post(path + "/response", json=command).status_code == 503
    assert all(d["state"] == "PENDING" for d in ok(c.get(path))["decisions"])
    fake.fail_at = None
    bad = {
        **command,
        "command_id": str(uuid.uuid4()),
        "decisions": [*choices, {"vehicle_id": "VEH-999999", "expected_version": 1, "state": "APPROVED"}],
    }
    assert c.post(path + "/response", json=bad).status_code == 422
    assert all(d["state"] == "PENDING" for d in ok(c.get(path))["decisions"])
    # Permission changes after staging are checked again, including direct FILE access.
    user.role, user.permissions = "OPERATOR", []
    db.commit()
    assert c.post(path + "/response", json=command).status_code == 403
    assert c.get(path + f"/files/{request['letter_file_id']}/download").status_code == 403
    assert c.get("/api/arm2/history/APPROVAL/" + request_id).status_code == 403


def test_0071_to_0072_preserves_originals_versions_history_and_links(qa, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.config import settings

    c, db, user, parents, kind, fake = qa
    path = f"/api/arm2/veh/{parents['veh']}/documents"
    document = ok(c.post(path, json={"kind_ref": kind, "name": "До миграции"}), 201)
    path += "/" + document["id"]
    ok(accept(c, path, 1, [stage(c, path, pdf("old-original"))]))
    db.commit()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = db.get_bind()

    def snapshot():
        with engine.connect() as connection:
            return [
                connection.execute(text(sql)).all()
                for sql in [
                    "SELECT id,name,plate_number,type_id,owner_cp_id,owner_org_id,version FROM arm2_vehicles ORDER BY id",
                    "SELECT row_to_json(d)::text FROM arm2_documents d ORDER BY id",
                    "SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id",
                    "SELECT id,document_id,storage_ref,sha256,size,stored_name,original_upload_name FROM arm2_document_files ORDER BY id",
                    "SELECT row_to_json(r)::text FROM arm2_document_revisions r ORDER BY id",
                ]
            ]

    expected = snapshot()
    command.downgrade(config, "0071")
    assert snapshot() == expected
    command.upgrade(config, "0072")
    assert snapshot() == expected
    db.expire_all()
    record = ok(c.get("/api/arm2/vehicles/" + parents["veh"]))
    assert record["calibration_m3"] is None and record["calibration_confirmed_at"] is None
    assert c.get(
        path
        + "/revisions/"
        + bundle(c, path)["current_revision_id"]
        + "/files/"
        + bundle(c, path)["revisions"][0]["files"][0]["id"]
        + "/download"
    ).content == pdf("old-original")
    settings.cache_clear()


def test_downgrade_refuses_to_discard_confirmation_or_approval_evidence(qa):
    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command

    c, db, *_ = qa
    prepare(qa)
    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "0071")
    db.rollback()
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0073"


def test_lost_commit_reply_uses_durable_receipt_not_duplicate_publication(qa, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.arm2 import approval_store

    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    original_receipt = approval_store.receipt
    lost = []

    def receipt(*args, **kwargs):
        result = original_receipt(*args, **kwargs)
        if not lost:
            lost.append(True)
            raise SQLAlchemyError("simulated lost acknowledgement after successful commit")
        return result

    monkeypatch.setattr(approval_store, "receipt", receipt)
    body, response = letter(c, vehicles)
    assert response.status_code == 503
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
    result = ok(c.post("/api/arm2/approvals/letter", json=body), 201)
    assert result["request_id"]
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
    assert db.scalar(select(func.count()).select_from(ApprovalDecision)) == 2
    assert len(fake.uploads) == 4


def test_duplicate_response_bytes_and_decisions_are_noop_and_other_request_file_is_denied(qa):
    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    _, response = letter(c, vehicles)
    path = "/api/arm2/approvals/" + ok(response, 201)["request_id"]
    initial = ok(c.get(path))
    stage_id = str(uuid.uuid4())
    ok(c.put(path + f"/response-stages/{stage_id}?name=answer.png", content=png()))

    def command(row, stage_id):
        return {
            "command_id": str(uuid.uuid4()),
            "expected_version": row["version"],
            "stage_id": stage_id,
            "decisions": [
                {"vehicle_id": d["vehicle_id"], "expected_version": d["version"], "state": "APPROVED"}
                for d in row["decisions"]
            ],
        }

    ok(c.post(path + "/response", json=command(initial, stage_id)))
    saved = ok(c.get(path))
    count = len(fake.uploads)
    second_stage = str(uuid.uuid4())
    ok(c.put(path + f"/response-stages/{second_stage}?name=same-bytes.png", content=png()))
    result = ok(c.post(path + "/response", json=command(saved, second_stage)))
    assert result["no_op"] and not result["event_ids"] and result["version"] == saved["version"]
    assert len(fake.uploads) == count
    assert ok(c.get(path)) == saved
    _, response = letter(c, vehicles)
    another = ok(response, 201)["request_id"]
    assert c.get(f"/api/arm2/approvals/{another}/files/{saved['letter_file_id']}/download").status_code == 404


def test_b4_and_b5_updates_cannot_write_or_erase_confirmed_volume(b5):
    from test_arm2_bulk import accept as bulk_accept
    from test_arm2_bulk import upload as bulk_upload
    from test_arm2_questionnaire_workflow import apply, choose, upload, workbook

    c, db, user, parents, kind, fake = b5
    vehicles = prepare(b5)
    vehicle_id = vehicles[0][0]
    record = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    proof = {key: value for key, value in record.items() if key.startswith("calibration_")}
    table = bulk_upload(c, f"Госномер\tМодель\n{record['plate_number']}\tB4 updated model")
    bulk_accept(c, table, fields={"model": 0})
    after_bulk = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    assert after_bulk["model"] == "B4 updated model"
    assert {key: after_bulk[key] for key in proof} == proof
    draft = choose(
        c,
        upload(
            c,
            workbook(
                rows=[{"B": vehicle_id, "E": record["plate_number"], "G": "B5 updated model", "L": "27"}]
            ),
        ),
        cp_target=parents["cp"],
        vehicles={"v:5": {"include": True, "fields": ["model"]}},
    )
    assert draft["comparison"]["vehicles"][0]["volume_proposal"] == "27"
    apply(c, draft)
    after_import = ok(c.get("/api/arm2/vehicles/" + vehicle_id))
    assert after_import["model"] == "B5 updated model"
    assert {key: after_import[key] for key in proof} == proof


def test_history_failure_rolls_back_whole_letter_and_retry_reuses_uploaded_originals(qa):
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)

    def fail_history(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("INSERT INTO arm2_history_events")
            and parameters.get("object_type") == "APPROVAL-ITEM"
        ):
            raise SQLAlchemyError("Synthetic failure before per-vehicle history publication")

    event.listen(db.get_bind(), "before_cursor_execute", fail_history)
    try:
        body, response = letter(c, vehicles)
        assert response.status_code == 503
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", fail_history)
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    assert db.scalar(select(func.count()).select_from(ApprovalDecision)) == 0
    assert (
        db.scalar(select(func.count()).select_from(DocumentFile).where(DocumentFile.process_id.is_not(None)))
        == 0
    )
    assert not db.scalars(select(HistoryEvent).where(HistoryEvent.object_type == "APPROVAL")).all()
    assert ok(c.post("/api/arm2/approvals/letter", json=body), 201)["request_id"]
    assert len(fake.uploads) == 4


def test_permission_revoked_during_original_upload_prevents_publication(qa, monkeypatch):
    from sqlalchemy.orm import Session

    from app.arm2 import document_file_storage
    from app.models import User

    c, db, user, parents, kind, fake = qa
    vehicles = prepare(qa)
    original = document_file_storage.ensure_original

    def upload_and_revoke(plan):
        original(plan)
        with Session(db.get_bind()) as other:
            actor = other.get(User, user.id)
            actor.role, actor.permissions = "OPERATOR", []
            other.commit()

    monkeypatch.setattr(document_file_storage, "ensure_original", upload_and_revoke)
    _, response = letter(c, vehicles)
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 0
    assert db.scalar(select(func.count()).select_from(ApprovalDecision)) == 0


def test_candidates_follow_shared_100_row_pagination_without_gaps(qa):
    from app.arm2.vehicle_models import Vehicle

    c, db, *_ = qa
    db.add_all(
        [
            Vehicle(
                id=f"VEH-{910000 + i:06}", name=f"Synthetic candidate {i}", plate_number=f"А{i + 200:03}АА86"
            )
            for i in range(105)
        ]
    )
    db.commit()
    first = ok(c.get("/api/arm2/approvals/candidates?offset=0"))
    second = ok(c.get("/api/arm2/approvals/candidates?offset=100"))
    assert len(first) == 100 and len(second) == 6
    first_ids = {row["vehicle_id"] for row in first}
    second_ids = {row["vehicle_id"] for row in second}
    assert not first_ids & second_ids and len(first_ids | second_ids) == 106
    assert all(not row["letter_eligible"] and row["reasons"] for row in [*first, *second])
