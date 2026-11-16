from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa

from app.arm2.history_models import HistoryEvent
from app.arm2.vehicle_models import Vehicle


def command(c, vehicle, target=None, *, expected=None, version=None, confirm=False, initiator=None, **extra):
    old = (
        expected
        if expected is not None
        else (
            ("CP", vehicle["owner_cp_id"])
            if vehicle["owner_cp_id"]
            else ("ORG", vehicle["owner_org_id"])
            if vehicle["owner_org_id"]
            else (None, None)
        )
    )
    return c.post(
        "/api/arm2/vehicles/" + vehicle["id"] + "/operational-owner",
        json={
            "expected_version": version or vehicle["version"],
            "target_kind": target[0] if target else None,
            "target_id": target[1] if target else None,
            "expected_owner_kind": old[0],
            "expected_owner_id": old[1],
            "confirm_reassignment": confirm,
            **({"initiator_kind": initiator[0], "initiator_id": initiator[1]} if initiator else {}),
            **extra,
        },
    )


def current(c, vid):
    r = c.get("/api/arm2/vehicles/" + vid)
    assert r.status_code == 200
    return r.json()


def count(db):
    return db.scalar(select(func.count()).select_from(HistoryEvent))


def test_full_owner_workflow_review_noop_and_b3_documents_are_unchanged(qa):
    c, db, user, parents, kind, disk = qa
    document = create(qa, "veh")
    assert accept(c, document, 1, [stage(c, document, pdf("immutable VEH original"))]).status_code == 200
    files = bundle(c, document)
    vehicle = current(c, parents["veh"])
    stable = {k: v for k, v in vehicle.items() if k not in ("version", "owner_cp_id", "owner_org_id")}
    cp2 = c.post("/api/arm2/counterparties", json={"name": "Second CP"}).json()["id"]
    org2 = c.post("/api/arm2/organizations", json={"name": "Second ORG"}).json()["id"]
    for target in [
        ("CP", parents["cp"]),
        ("CP", cp2),
        ("ORG", parents["org"]),
        ("ORG", org2),
        ("CP", parents["cp"]),
        None,
    ]:
        before = count(db)
        owner = (
            ("CP", vehicle["owner_cp_id"])
            if vehicle["owner_cp_id"]
            else ("ORG", vehicle["owner_org_id"])
            if vehicle["owner_org_id"]
            else None
        )
        if owner:
            assert command(c, vehicle, target).status_code == 409
            assert current(c, vehicle["id"]) == vehicle and count(db) == before
        r = command(c, vehicle, target, confirm=bool(owner), initiator=target or owner)
        assert r.status_code == 200 and r.json()["no_op"] is False
        fresh = current(c, vehicle["id"])
        assert fresh["version"] == vehicle["version"] + 1
        assert fresh["owner_cp_id"] == (target[1] if target and target[0] == "CP" else None)
        assert fresh["owner_org_id"] == (target[1] if target and target[0] == "ORG" else None)
        assert {k: fresh[k] for k in stable} == stable
        assert count(db) == before + 1
        e = c.get("/api/arm2/history/VEH/" + vehicle["id"]).json()[0]
        detail = c.get("/api/arm2/history/events/" + str(e["event_id"])).json()
        assert set(detail["changes"]) <= {"version", "owner_cp_id", "owner_org_id"}
        assert detail["source"] == "arm2-api-operational-owner" and detail["actor_id"] == str(user.id)
        assert bundle(c, document) == files and len(disk.uploads) == 1
        vehicle = fresh
        assert command(c, vehicle, target, initiator=target or owner).json()["no_op"] is True
        assert current(c, vehicle["id"]) == vehicle and count(db) == before + 1
    assert c.get("/api/arm2/vehicles/search?owner_kind=NONE").json()["items"][0]["id"] == vehicle["id"]
    # REVIEW is a description state, never a reason to clear or deny an owner link.
    assert command(c, vehicle, ("CP", parents["cp"])).status_code == 200
    from app.arm2.counterparty_models import Counterparty

    owner = db.get(Counterparty, parents["cp"])
    owner.full_name = "Full name"
    owner.legal_address = "Address"
    db.commit()
    body = c.get("/api/arm2/counterparties/" + owner.id).json()
    assert (
        c.put(
            "/api/arm2/counterparties/" + owner.id,
            json={
                "expected_version": body["version"],
                "name": body["name"],
                "full_name": "Full name",
                "legal_address": "Address",
            },
        ).json()["status"]
        == "CONFIRMED"
    )
    body = c.get("/api/arm2/counterparties/" + owner.id).json()
    assert (
        c.put(
            "/api/arm2/counterparties/" + owner.id,
            json={"expected_version": body["version"], "name": body["name"], "full_name": None},
        ).json()["status"]
        == "REVIEW"
    )
    assert current(c, vehicle["id"])["owner_cp_id"] == owner.id


@pytest.mark.parametrize(
    "grants",
    [
        ["arm2_veh_view"],
        ["arm2_veh_edit"],
        ["arm2_veh_view", "arm2_veh_edit", "arm2_org_view", "arm2_org_edit"],
        ["arm2_veh_view", "arm2_veh_edit", "arm2_cp_view", "arm2_org_view", "arm2_org_edit"],
        ["arm2_veh_view", "arm2_veh_edit", "arm2_cp_view", "arm2_cp_edit", "arm2_org_view"],
    ],
)
def test_owner_command_uses_actual_previous_and_target_permissions_without_name_leak(qa, grants):
    c, db, user, parents, *_ = qa
    vehicle = current(c, parents["veh"])
    assert command(c, vehicle, ("CP", parents["cp"])).status_code == 200
    vehicle = current(c, parents["veh"])
    before = count(db)
    user.role = "OPERATOR"
    user.permissions = grants
    db.commit()
    r = command(c, vehicle, ("ORG", parents["org"]), confirm=True)
    assert r.status_code == 403
    assert "CP-000001" not in r.text and "ORG-000001" not in r.text
    # Lying about expected_owner never avoids the locked actual-owner permission check.
    assert (
        command(c, vehicle, ("ORG", parents["org"]), expected=(None, None), confirm=True).status_code == 403
    )
    db.expire_all()
    record = db.get(Vehicle, vehicle["id"])
    assert (
        record.owner_cp_id == parents["cp"] and record.version == vehicle["version"] and count(db) == before
    )
    if "arm2_veh_view" in grants and "arm2_cp_view" not in grants:
        search = c.get("/api/arm2/vehicles/search").json()["items"][0]
        assert search["owner_name"] is None


def test_stale_owner_target_tampering_invalid_inputs_and_confirmation_have_no_side_effects(qa):
    c, db, user, parents, *_ = qa
    vehicle = current(c, parents["veh"])
    assert command(c, vehicle, ("CP", parents["cp"])).status_code == 200
    vehicle = current(c, parents["veh"])
    before = count(db)
    assert command(c, vehicle, ("ORG", parents["org"]), version=1, confirm=True).status_code == 409
    assert (
        command(c, vehicle, ("ORG", parents["org"]), expected=(None, None), confirm=True).status_code == 409
    )
    assert command(c, vehicle, ("CP", "CP-999999"), confirm=True).status_code == 404
    assert command(c, vehicle, ("ORG", parents["cp"]), confirm=True).status_code == 422
    assert command(c, vehicle, (None, parents["cp"]), confirm=True).status_code == 422
    assert command(c, vehicle, ("ORG", parents["org"]), confirm="true").status_code == 422
    assert (
        command(c, vehicle, ("ORG", parents["org"]), confirm=True, owner_cp_id=parents["cp"]).status_code
        == 422
    )
    unrelated = c.post("/api/arm2/counterparties", json={"name": "Unrelated initiator"}).json()["id"]
    before = count(db)
    assert (
        command(
            c, vehicle, ("ORG", parents["org"]), confirm=True, initiator=("CP", parents["cp"])
        ).status_code
        == 422
    )
    assert (
        command(c, vehicle, ("ORG", parents["org"]), confirm=True, initiator=("CP", unrelated)).status_code
        == 422
    )
    assert current(c, vehicle["id"]) == vehicle and count(db) == before


def test_two_authenticated_sessions_get_one_owner_and_one_409_then_reconfirm(qa):
    c, db, user, parents, *_ = qa
    from app.api import app
    from app.auth import hasher
    from app.models import User

    vehicle = current(c, parents["veh"])
    second = User(
        login="owner-second",
        name="owner-second",
        role="OPERATOR",
        active=True,
        permissions=[
            "arm2_veh_view",
            "arm2_veh_edit",
            "arm2_cp_view",
            "arm2_cp_edit",
            "arm2_org_view",
            "arm2_org_edit",
        ],
        password_hash=hasher.hash("qa-link-password"),
    )
    db.add(second)
    db.commit()
    before = count(db)
    with TestClient(app) as other:
        assert (
            other.post(
                "/api/auth/login", json={"login": "owner-second", "password": "qa-link-password"}
            ).status_code
            == 200
        )
        other.headers["X-CSRF-Token"] = other.cookies["csrf"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(command, client, vehicle, target)
                for client, target in ((c, ("CP", parents["cp"])), (other, ("ORG", parents["org"])))
            ]
            assert sorted(f.result(timeout=15).status_code for f in futures) == [200, 409]
        assert count(db) == before + 1
        fresh = current(c, vehicle["id"])
        target = ("ORG", parents["org"]) if fresh["owner_cp_id"] else ("CP", parents["cp"])
        assert command(other, fresh, target, confirm=True).status_code == 200
        assert count(db) == before + 2
    assert db.scalar(select(func.count()).select_from(Vehicle)) == 1


def test_permission_revoked_while_waiting_for_vehicle_row_lock_is_checked_again(qa, monkeypatch):
    from threading import Event

    from app.arm2.vehicle_store import VEHICLE_ADAPTER

    c, db, user, parents, *_ = qa
    vehicle = current(c, parents["veh"])
    before = count(db)
    db.scalar(select(Vehicle).where(Vehicle.id == vehicle["id"]).with_for_update())
    waiting = Event()
    original = VEHICLE_ADAPTER.lock_record

    def wait_for_record(session, identity):
        waiting.set()
        return original(session, identity)

    monkeypatch.setattr(VEHICLE_ADAPTER, "lock_record", wait_for_record)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(command, c, vehicle, ("CP", parents["cp"]))
        assert waiting.wait(10)
        user.role = "OPERATOR"
        user.permissions = ["arm2_veh_view"]
        db.commit()
        assert result.result(timeout=15).status_code == 403
    assert current(c, vehicle["id"]) == vehicle and count(db) == before


@pytest.mark.parametrize("failure", ["history", "commit"])
def test_write_failure_rolls_back_owner_and_history_and_allows_safe_retry(qa, monkeypatch, failure):
    c, db, user, parents, *_ = qa
    from sqlalchemy.orm import Session

    import app.arm2.vehicle_store as store

    original = store.update_object
    vehicle = current(c, parents["veh"])
    before = count(db)

    def fail_writer(*_args, **_kwargs):
        raise RuntimeError("isolated history failure")

    def save(*args, **kwargs):
        if failure == "history":
            kwargs["history_writer"] = fail_writer
        result = original(*args, **kwargs)
        if failure == "commit":
            args[0].info["fail_owner_commit"] = True
        return result

    def before_commit(session):
        if session.info.pop("fail_owner_commit", False):
            raise RuntimeError("isolated commit failure")

    monkeypatch.setattr(store, "update_object", save)
    event.listen(Session, "before_commit", before_commit)
    try:
        with pytest.raises(RuntimeError, match="isolated"):
            command(c, vehicle, ("CP", parents["cp"]))
    finally:
        event.remove(Session, "before_commit", before_commit)
        monkeypatch.setattr(store, "update_object", original)
    assert current(c, vehicle["id"]) == vehicle and count(db) == before
    assert command(c, vehicle, ("CP", parents["cp"])).status_code == 200
    assert count(db) == before + 1


def test_owner_command_never_silently_normalizes_other_legacy_vehicle_fields(qa):
    c, db, user, parents, *_ = qa
    record = db.get(Vehicle, parents["veh"])
    record.model = "  Legacy model with spaces  "
    db.commit()
    before = current(c, record.id)
    history = count(db)
    response = command(c, before, ("CP", parents["cp"]))
    assert response.status_code == 422
    assert current(c, record.id) == before
    assert count(db) == history


def test_existing_server_vehicle_search_pages_beyond_first_hundred_and_keeps_ids(qa):
    c, db, user, parents, *_ = qa
    from app.arm2.object_save import SaveContext
    from app.arm2.vehicle_store import create_vehicle

    for i in range(105):
        create_vehicle(
            db,
            "Техника " + str(i).zfill(3),
            f"А{i:03}ВС86",
            principal=user,
            context=SaveContext("HUMAN", str(user.id), "isolated-owner-search"),
        )
    db.commit()
    page = c.get("/api/arm2/vehicles/search?limit=100&offset=100").json()
    assert len(page["items"]) == 6 and page["total"] == 106
    match = c.get("/api/arm2/vehicles/search?q=Техника%20104").json()
    assert match["matched"] == 1
    vehicle = match["items"][0]
    response = command(c, vehicle, ("ORG", parents["org"]), initiator=("ORG", parents["org"]))
    assert response.status_code == 200 and response.json()["id"] == vehicle["id"]
    assert (
        c.get("/api/arm2/vehicles/search?owner_kind=ORG&owner_id=" + parents["org"]).json()["items"][0]["id"]
        == vehicle["id"]
    )
