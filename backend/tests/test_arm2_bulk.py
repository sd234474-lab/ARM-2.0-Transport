import copy
import io
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.arm2.bulk_models import BulkCleanup, BulkReceipt, BulkSession
from app.arm2.bulk_parse import MANIFEST, parse
from app.arm2.object_save import SaveContext, update_object
from app.arm2.routes import router
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle
from app.arm2.vehicle_type_store import create_vehicle_type
from app.auth import current_user
from app.config import settings
from app.db import get_db
from app.models import User


@pytest.fixture
def bulk(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("ARM2_TABLE_SIGNING_KEY", "test-only-signing-key-not-a-live-secret-45")
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url)
    with Session(engine) as db:
        users = [User(login=f"bulk-{i}", name=f"Оператор {i}", role="ADMIN_OPERATOR", active=True, password_hash="unused") for i in range(2)]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]
    actor = {"id": ids[0]}
    app = FastAPI()
    app.include_router(router)

    def database():
        with Session(engine, expire_on_commit=False) as db:
            yield db

    def authenticated(db: Annotated[Session, Depends(get_db)]):
        return db.get(User, actor["id"])

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[current_user] = authenticated
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, engine, actor, ids, app
    engine.dispose()


def upload(client, content, filename="paste.tsv", **query):
    response = client.post("/api/arm2/bulk/sessions", params={"filename": filename, **query}, content=content.encode() if isinstance(content, str) else content)
    assert response.status_code == 201, response.text
    return response.json()


def edit(client, w, **changes):
    body = {k: w[k] for k in ("revision", "headers", "rows", "mapping", "excluded", "clears", "decisions", "confirmations")}
    body.update(changes)
    response = client.put(f"/api/arm2/bulk/sessions/{w['id']}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def apply_body(w, plan=None, fields=None, **extra):
    p = plan or w["plans"][0]
    return dict(command_id=str(uuid4()), preview_id=w["preview_id"], row_key=p["key"],
                fields=fields if fields is not None else {f: 0 for f, options in p["fields"].items() if not options[0]["error"]}, **extra)


def accept(client, w, plan=None, fields=None, **extra):
    body = apply_body(w, plan, fields, **extra)
    response = client.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def reopen(client, w):
    response = client.get(f"/api/arm2/bulk/sessions/{w['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def seed(engine, user_id, count=1):
    with Session(engine) as db:
        user = db.get(User, user_id)
        result = [create_vehicle(db, f"Техника {i}", f"А{i:03}АА86", principal=user,
                                 context=SaveContext("HUMAN", str(user.id), "seed")) for i in range(count)]
        db.commit()
        return [r.id for r in result]


def book_bytes(book):
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def counts(engine):
    from app.arm2.history_models import HistoryEvent
    with Session(engine) as db:
        return tuple(db.scalar(select(func.count()).select_from(m)) for m in (Vehicle, HistoryEvent, BulkReceipt))


def test_full_filtered_export_text_cells_and_signed_noop(bulk):
    c, engine, _, ids, _ = bulk
    seed(engine, ids[0], 105)
    exported = c.get("/api/arm2/bulk/vehicles/export?state=Действующая&sort=plate_number&direction=desc")
    assert exported.status_code == 200, exported.text
    book = load_workbook(io.BytesIO(exported.content))
    assert book.active.max_row == 106 and MANIFEST in book.sheetnames
    assert all(cell.number_format == "@" and (cell.value is None or cell.data_type == "s") for cell in book.active[2])
    assert "А104АА86" in [cell.value for cell in book.active[2]]
    assert not any("документ" in str(cell.value).lower() for cell in book.active[1])
    before = counts(engine)
    w = upload(c, exported.content, "roundtrip.xlsx")
    assert all(p["action"] == "SKIP" for p in w["plans"])
    assert "Подлинный" in w["warning"] and counts(engine) == before
    assert c.get("/api/arm2/bulk/vehicles/export?limit=100").status_code == 422


def test_create_default_name_normalization_duplicate_groups_and_independent_errors(bulk):
    c, engine, _, _, _ = bulk
    w = upload(c, "Госномер\tРабочее имя\tTYPE — ID\n a 123 bc 86\tПервая\tTYPE-999999\nА123ВС86\tВторая\t\nJ999JJ86\tНеверная\t")
    assert len(w["plans"]) == 2 and counts(engine) == (0, 0, 0)
    p = next(p for p in w["plans"] if p["key"] == "А123ВС86")
    assert p["sources"] == [2, 3] and len(p["fields"]["name"]) == 2
    assert p["fields"]["type_id"][0]["error"] and p["fields"]["name"][0]["conflict"]
    result, body = accept(c, w, p, {"plate_number": 0, "name": 1})
    assert counts(engine) == (1, 1, 1)
    with Session(engine) as db:
        v = db.get(Vehicle, result["vehicle_id"])
        assert (v.name, v.plate_number, v.type_id, v.owner_cp_id, v.accounting_state, v.version) == ("Вторая", "А123ВС86", None, None, "Действующая", 1)
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body).json() == result
    body["fields"] = {"plate_number": 0}
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body).status_code == 409
    repeated = upload(c, "Госномер\nA123BC86")
    assert repeated["plans"][0]["action"] == "SKIP" and counts(engine) == (1, 1, 1)
    default = upload(c, "plate_number\nB124BC86", "one.csv")
    accepted, _ = accept(c, default)
    with Session(engine) as db:
        assert db.get(Vehicle, accepted["vehicle_id"]).name == "В124ВС86"


def test_mapping_edits_exclusion_bulk_values_and_private_restart(bulk):
    c, engine, actor, ids, _ = bulk
    w = upload(c, "Unknown;Other;Extra\nA111AA86;старое;лишнее\nA112AA86;старое;лишнее", "table.csv")
    assert w["mapping"] == ["", "", ""]
    rows = copy.deepcopy(w["rows"])
    for row in rows:
        row[1] = "Массовое значение"
    w = edit(c, w, mapping=["plate_number", "name", ""], rows=rows, excluded=[1])
    assert len(w["plans"]) == 1 and counts(engine) == (0, 0, 0)
    with engine.begin() as db:
        db.execute(text("UPDATE arm2_bulk_sessions SET created_at='2000-01-01' WHERE id=:id"), {"id": w["id"]})
    engine.dispose()
    restored = reopen(c, w)
    assert restored["rows"] == rows and restored["mapping"] == ["plate_number", "name", ""] and restored["excluded"] == [1]
    actor["id"] = ids[1]
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}").status_code == 403
    assert not c.get("/api/arm2/bulk/sessions").json()
    actor["id"] = ids[0]
    result, _ = accept(c, restored)
    with Session(engine) as db:
        assert db.get(Vehicle, result["vehicle_id"]).name == "Массовое значение"


def test_signed_three_way_preserves_unedited_owner_and_resolves_name_conflict(bulk):
    from app.arm2.counterparty_models import Counterparty
    from app.arm2.history_models import HistoryEvent
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    exported = c.get("/api/arm2/bulk/vehicles/export").content
    book = load_workbook(io.BytesIO(exported))
    col = {cell.value: cell.column for cell in book.active[1]}
    book.active.cell(2, col["Рабочее имя"]).value = "Excel"
    changed = book_bytes(book)
    with Session(engine) as db:
        db.add(Counterparty(id="CP-800001", name="Владелец", status="REVIEW", version=1))
        db.flush()
        update_object(db, object_type="VEH", object_id=vid, expected_version=1, partial=True,
                      values={"owner_cp_id": "CP-800001", "owner_org_id": None}, principal=db.get(User, ids[0]),
                      context=SaveContext("HUMAN", str(ids[0]), "other-operator"))
        db.commit()
    w = upload(c, changed, "own.xlsx")
    assert set(w["plans"][0]["fields"]) == {"name"}
    accept(c, w)
    with Session(engine) as db:
        v = db.get(Vehicle, vid)
        assert (v.name, v.owner_cp_id, v.version) == ("Excel", "CP-800001", 3)
        event = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == vid).order_by(HistoryEvent.event_id.desc()))
        assert set(event.changes) == {"name", "version"}
    book.active.cell(2, col["Рабочее имя"]).value = "Другой Excel"
    conflict = upload(c, book_bytes(book), "older.xlsx")
    assert conflict["plans"][0]["fields"]["name"][0]["conflict"]
    accept(c, conflict, fields={"name": 0})
    with Session(engine) as db:
        assert db.get(Vehicle, vid).name == "Другой Excel"
    # Tampering invalidates trust; supplied version is never an expected_version.
    signed = book[MANIFEST].cell(1, 1)
    signed.value = signed.value.replace('"schema":1', '"schema":9')
    bad = upload(c, book_bytes(book), "tampered.xlsx")
    assert "не подтверждена" in bad["warning"]
    assert all(not o["has_original"] for p in bad["plans"] for opts in p["fields"].values() for o in opts)


def test_id_plate_conflict_plate_confirmation_and_version_409(bulk):
    c, engine, actor, ids, _ = bulk
    first, second = seed(engine, ids[0], 2)
    conflict = upload(c, f"id\tplate_number\tname\n{first}\tА001АА86\tНельзя")
    assert conflict["plans"][0]["errors"]
    assert c.post(f"/api/arm2/bulk/sessions/{conflict['id']}/apply", json=apply_body(conflict, fields={"name": 0})).status_code == 422
    w = upload(c, f"id\tplate_number\n{first}\tА777АА86")
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply_body(w)).status_code == 422
    accept(c, w, confirm_plate=True)
    with Session(engine) as db:
        assert db.get(Vehicle, first).plate_number == "А777АА86"
    one = upload(c, f"id\tname\n{second}\tПервый")
    actor["id"] = ids[1]
    two = upload(c, f"id\tname\n{second}\tВторой")
    actor["id"] = ids[0]
    accept(c, one)
    actor["id"] = ids[1]
    assert c.post(f"/api/arm2/bulk/sessions/{two['id']}/apply", json=apply_body(two)).status_code == 409
    fresh = reopen(c, two)
    accept(c, fresh)
    with Session(engine) as db:
        assert (db.get(Vehicle, second).name, db.get(Vehicle, second).version) == ("Второй", 3)


def test_nullable_clear_does_not_clear_neighbors_and_requires_owner_confirmation(bulk):
    from app.arm2.counterparty_models import Counterparty
    c, engine, _, ids, _ = bulk
    with Session(engine) as db:
        user = db.get(User, ids[0])
        typ = create_vehicle_type(db, "Авто", True, False, False, plate_category="Автомобильный", principal=user, context=SaveContext("HUMAN", str(user.id), "seed"))
        db.add(Counterparty(id="CP-800001", name="Старый", status="REVIEW", version=1))
        db.flush()
        v = create_vehicle(db, "Имя", "А333АА86", type_id=typ.id, model="Модель", owner_cp_id="CP-800001", wialon_unit_id="00012", principal=user, context=SaveContext("HUMAN", str(user.id), "seed"))
        db.commit()
        vid = v.id
    w = upload(c, f"id\tmodel\ttype_id\towner_cp_id\twialon_unit_id\n{vid}\t\t\t\t")
    assert w["plans"][0]["action"] == "SKIP"
    w = edit(c, w, clears={"0": ["type_id"]})
    accept(c, w)
    with Session(engine) as db:
        v = db.get(Vehicle, vid)
        assert (v.type_id, v.owner_cp_id, v.model, v.wialon_unit_id, v.name) == (None, "CP-800001", "Модель", "00012", "Имя")
    w = reopen(c, w)
    w = edit(c, w, clears={"0": ["owner", "model", "wialon_unit_id"]})
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply_body(w)).status_code == 422
    accept(c, w, confirm_owner=True)
    with Session(engine) as db:
        v = db.get(Vehicle, vid)
        assert (v.type_id, v.owner_cp_id, v.owner_org_id, v.model, v.wialon_unit_id, v.name, v.plate_number) == (None, None, None, None, None, "Имя", "А333АА86")
    body = {k: w[k] for k in ("revision", "headers", "rows", "mapping", "excluded", "clears", "decisions", "confirmations")}
    body["clears"] = {"0": ["name"]}
    assert c.put(f"/api/arm2/bulk/sessions/{w['id']}", json=body).status_code == 422


def test_reference_name_conflicts_category_and_partial_field_acceptance(bulk):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    with Session(engine) as db:
        user = db.get(User, ids[0])
        typ = create_vehicle_type(db, "Трактор", True, False, False, plate_category="Самоходная техника", principal=user, context=SaveContext("HUMAN", str(user.id), "seed"))
        db.commit()
        tid = typ.id
    w = upload(c, f"id\tmodel\ttype_id\ttype_name\n{vid}\tДопустимо\t{tid}\tТрактор")
    p = w["plans"][0]
    assert p["fields"]["type_id"][0]["error"] and not p["fields"]["model"][0]["error"]
    accept(c, w, fields={"model": 0})
    with Session(engine) as db:
        assert db.get(Vehicle, vid).model == "Допустимо" and db.get(Vehicle, vid).type_id is None
    wrong = upload(c, f"id\ttype_id\ttype_name\n{vid}\t{tid}\tЧужое название")
    assert "ID и наименование" in wrong["plans"][0]["fields"]["type_id"][0]["error"]


def test_view_export_permissions_foreign_workspace_and_admin_cleanup(bulk):
    c, engine, actor, ids, _ = bulk
    w = upload(c, "plate_number\nА555АА86")
    accepted, _ = accept(c, w)
    actor["id"] = ids[1]
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}").status_code == 403
    with Session(engine) as db:
        user = db.get(User, ids[1])
        user.role, user.permissions = "OPERATOR", ["arm2_veh_view"]
        db.commit()
    book = load_workbook(io.BytesIO(c.get("/api/arm2/bulk/vehicles/export").content))
    headers = [cell.value for cell in book.active[1]]
    assert "TYPE — ID" not in headers and "Владелец — ID" not in headers
    assert c.post("/api/arm2/bulk/sessions?filename=paste.tsv", content="plate_number\nА556АА86").status_code == 403
    assert c.get("/api/arm2/bulk/cleanup").status_code == 403
    with Session(engine) as db:
        db.get(User, ids[1]).role = "ADMIN_OPERATOR"
        db.commit()
    assert c.post(f"/api/arm2/bulk/cleanup/{w['id']}", json={"confirm_session": w["id"]}).status_code == 200
    with Session(engine) as db:
        assert db.get(Vehicle, accepted["vehicle_id"]) is not None
        assert db.scalar(select(func.count()).select_from(BulkReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(BulkCleanup)) == 1
    actor["id"] = ids[0]
    assert c.get("/api/arm2/bulk/protocols").json()[0]["results"][0]["status"] == "SAVED"


def test_63_commits_disconnect_resume_37_then_finish_protocol(bulk):
    c, engine, _, _, _ = bulk
    w = upload(c, "plate_number\n" + "\n".join(f"А{i:03}ВС86" for i in range(100)))
    last = None
    for p in w["plans"][:63]:
        _, last = accept(c, w, p)
    assert counts(engine) == (63, 63, 63)
    # Lost response: retransmit the identical last command; no duplicate HIST.
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=last).status_code == 200
    engine.dispose()
    resumed = reopen(c, w)
    assert len(resumed["results"]) == 63 and not resumed["blocked"]
    remaining = [p for p in resumed["plans"] if p["action"] == "CREATE"]
    assert len(remaining) == 37 and counts(engine) == (63, 63, 63)
    for p in remaining:
        accept(c, resumed, p)
    assert counts(engine) == (100, 100, 100)
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}/report").status_code == 200
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": resumed["revision"], "cancel": False})
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 100
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}").status_code == 404
    assert len(c.get("/api/arm2/bulk/protocols").json()[0]["results"]) == 100
    assert counts(engine) == (100, 100, 100)


def test_unknown_receipt_blocks_retry_finish_and_cleanup(bulk):
    c, engine, _, _, _ = bulk
    w = upload(c, "plate_number\nА101ВС86\nА102ВС86")
    first, body = accept(c, w, w["plans"][0])
    second, _ = accept(c, w, w["plans"][1])
    with Session(engine) as db:
        db.get(BulkReceipt, first["command_id"]).event_id = second["event_id"]
        db.commit()
    restored = reopen(c, w)
    assert restored["blocked"] and restored["results"][0]["status"] == "UNKNOWN"
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body).status_code == 409
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": w["revision"]}).status_code == 409
    assert c.post(f"/api/arm2/bulk/cleanup/{w['id']}", json={"confirm_session": w["id"]}).status_code == 409
    assert counts(engine) == (2, 2, 2)


def test_history_failure_and_lost_commit_ack_are_reconciled(bulk, monkeypatch):
    from app.arm2 import bulk_routes
    c, engine, _, _, app = bulk
    w = upload(c, "plate_number\nА666ВС86")
    original_create = bulk_routes.create_object
    def fail_history(*args, **kwargs):
        def fail(*a, **kw):
            raise RuntimeError("synthetic history failure")
        return original_create(*args, **kwargs, history_writer=fail)
    monkeypatch.setattr(bulk_routes, "create_object", fail_history)
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply_body(w))
    assert response.status_code == 500 and counts(engine) == (0, 0, 0)
    monkeypatch.setattr(bulk_routes, "create_object", original_create)
    resumed = reopen(c, w)
    class LostAck(Session):
        def commit(self):
            is_save = any(isinstance(obj, BulkReceipt) for obj in self.identity_map.values())
            super().commit()
            if is_save:
                raise ConnectionError("synthetic lost commit acknowledgement")
    def database():
        with LostAck(engine, expire_on_commit=False) as db:
            yield db
    original_db = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = database
    body = apply_body(resumed)
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body)
    assert response.status_code == 500 and counts(engine) == (1, 1, 1)
    app.dependency_overrides[get_db] = original_db
    recovered = reopen(c, w)
    assert recovered["results"][0]["status"] == "SAVED"
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body).status_code == 200
    assert counts(engine) == (1, 1, 1)


def test_cancel_before_save_no_protocol_and_finish_requires_download(bulk):
    c, engine, _, _, _ = bulk
    w = upload(c, "plate_number\nА567ВС86\nНеверный")
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": w["revision"]})
    assert response.status_code == 409
    report = c.get(f"/api/arm2/bulk/sessions/{w['id']}/report")
    assert load_workbook(io.BytesIO(report.content)).active.max_row == 3
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": w["revision"], "cancel": True})
    assert response.status_code == 200 and counts(engine) == (0, 0, 0)
    assert not c.get("/api/arm2/bulk/protocols").json()


def test_xlsx_sheet_selection_and_formula_size_guards(bulk):
    c, engine, _, _, _ = bulk
    book = Workbook()
    book.active.append(["plate_number"])
    book.active.append(["А678ВС86"])
    book.create_sheet("Другой").append(["Не выбран"])
    raw = book_bytes(book)
    response = upload(c, raw, "multi.xlsx")
    assert response["sheets"] == ["Sheet", "Другой"]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BulkSession)) == 0
    w = upload(c, raw, "multi.xlsx", sheet="Sheet")
    assert w["plans"][0]["action"] == "CREATE"
    book.active.cell(2, 1).value = "=1+1"
    assert c.post("/api/arm2/bulk/sessions?filename=bad.xlsx&sheet=Sheet", content=book_bytes(book)).status_code == 422
    assert c.post("/api/arm2/bulk/sessions?filename=bad.xlsx", content=b"bad ZIP").status_code == 422
    assert c.post("/api/arm2/bulk/sessions?filename=big.csv", content=b"x" * (5*1024*1024+1)).status_code == 413
    for content in ("plate_number\n" + "\n".join("А999АА86" for _ in range(1001)), "\t".join("x" for _ in range(33))):
        with pytest.raises(ValueError):
            parse(content.encode(), "paste.tsv")


def test_duplicate_equal_to_current_remains_a_conflict_and_approvals_reset(bulk):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    w = upload(c, f"id\tname\n{vid}\tТехника 0\n{vid}\tАльтернатива")
    options = w["plans"][0]["fields"]["name"]
    assert len(options) == 2 and all(o["conflict"] for o in options)
    assert {o["value"] for o in options} == {"Техника 0", "Альтернатива"}
    index = next(i for i, o in enumerate(options) if o["value"] == "Альтернатива")
    w = edit(c, w, decisions={vid: {"name": index}})
    assert w["decisions"] == {vid: {"name": index}}
    assert reopen(c, w)["decisions"] == w["decisions"]
    with Session(engine) as db:
        update_object(db, object_type="VEH", object_id=vid, expected_version=1, values={"name": "Другой оператор"},
                      partial=True, principal=db.get(User, ids[0]), context=SaveContext("HUMAN", str(ids[0]), "other"))
        db.commit()
    restored = reopen(c, w)
    assert restored["approval_reset"] and restored["decisions"] == {}
    assert counts(engine) == (1, 2, 0)


def test_revoked_reference_rights_and_snapshot_redaction_at_apply(bulk):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    with Session(engine) as db:
        user = db.get(User, ids[0])
        typ = create_vehicle_type(db, "Допустимый", True, False, False, plate_category="Автомобильный", principal=user,
                                  context=SaveContext("HUMAN", str(user.id), "seed"))
        db.commit()
        tid = typ.id
    w = upload(c, f"id\ttype_id\tmodel\n{vid}\t{tid}\tИмпортированная модель")
    assert not w["plans"][0]["fields"]["type_id"][0]["error"]
    with Session(engine) as db:
        user = db.get(User, ids[0])
        user.role, user.permissions = "OPERATOR", ["arm2_veh_view", "arm2_veh_edit"]
        db.commit()
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply_body(w, fields={"type_id": 0})).status_code == 422
    fresh = reopen(c, w)
    assert fresh["plans"][0]["fields"]["type_id"][0]["current"] is None
    accept(c, fresh, fields={"model": 0})
    with Session(engine) as db:
        v = db.get(Vehicle, vid)
        assert v.type_id is None and v.model == "Импортированная модель"


def test_zip_bomb_macros_external_links_xml_and_timeout_are_rejected(bulk, monkeypatch):
    import subprocess
    import zipfile

    from app.arm2 import bulk_tables
    c, engine, _, _, _ = bulk
    for name, content in [("xl/worksheets/sheet1.xml", b"x" * (20*1024*1024+1)),
                          ("xl/vbaProject.bin", b"macro"), ("xl/externalLinks/link1.xml", b"link"),
                          ("xl/worksheets/sheet1.xml", b'<!DOCTYPE test [<!ENTITY x "boom">]><test/>')]:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(name, content)
        assert c.post("/api/arm2/bulk/sessions?filename=bad.xlsx", content=archive.getvalue()).status_code == 422
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 8)
    monkeypatch.setattr(bulk_tables.subprocess, "run", timeout)
    assert c.post("/api/arm2/bulk/sessions?filename=slow.tsv", content=b"plate_number\nA999AA86").status_code == 422
    assert counts(engine) == (0, 0, 0)


def test_missing_signing_key_fails_export_and_csv_version_is_not_trusted(bulk, monkeypatch):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    monkeypatch.setenv("ARM2_TABLE_SIGNING_KEY", "")
    settings.cache_clear()
    assert c.get("/api/arm2/bulk/vehicles/export").status_code == 503
    w = upload(c, f"id\tversion\tname\n{vid}\t99999\tЯвно принято")
    assert w["plans"][0]["version"] == 1
    accept(c, w)
    with Session(engine) as db:
        assert (db.get(Vehicle, vid).name, db.get(Vehicle, vid).version) == ("Явно принято", 2)


def test_report_must_be_downloaded_again_if_arm_changes_before_finish(bulk):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    w = upload(c, f"id\tname\n{vid}\tНе принято")
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}/report").status_code == 200
    with Session(engine) as db:
        update_object(db, object_type="VEH", object_id=vid, expected_version=1, partial=True,
                      values={"name": "Конкурентное изменение"}, principal=db.get(User, ids[0]),
                      context=SaveContext("HUMAN", str(ids[0]), "other"))
        db.commit()
    response = c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": w["revision"]})
    assert response.status_code == 409 and "Отчёт устарел" in response.text
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}/report").status_code == 200
    assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/finish", json={"revision": w["revision"]}).status_code == 200
    assert counts(engine) == (1, 2, 0)


def test_explicit_clear_is_not_suppressed_by_an_original_null_snapshot(bulk):
    c, engine, _, ids, _ = bulk
    vid = seed(engine, ids[0])[0]
    exported = c.get("/api/arm2/bulk/vehicles/export").content
    with Session(engine) as db:
        update_object(db, object_type="VEH", object_id=vid, expected_version=1, partial=True,
                      values={"model": "Новое значение ARM"}, principal=db.get(User, ids[0]),
                      context=SaveContext("HUMAN", str(ids[0]), "other"))
        db.commit()
    w = upload(c, exported, "own.xlsx")
    assert w["plans"][0]["action"] == "SKIP"  # Blank Excel still preserves ARM.
    w = edit(c, w, clears={"0": ["model"]})
    option = w["plans"][0]["fields"]["model"][0]
    assert option["clear"] and option["conflict"] and option["original"] is None
    accept(c, w, fields={"model": 0})
    with Session(engine) as db:
        v = db.get(Vehicle, vid)
        assert v.model is None and v.version == 3
