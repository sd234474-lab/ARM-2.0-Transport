from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.api import app
from app.arm2 import routes as arm2_routes
from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext, create_object
from app.arm2.organization_store import create_organization
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.models import User
from app.permissions import Permission


def set_operator(db, permissions, *, active=True):
    operator = db.scalar(select(User).where(User.login == "operator"))
    operator.permissions = permissions
    operator.active = active
    db.commit()
    return operator


def sign_in(client):
    response = client.post(
        "/api/auth/login",
        json={"login": "operator", "password": "test-password-123"},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["csrf"]
    return response.json()


def server_context(user):
    return SaveContext("HUMAN", str(user.id), "route-test-setup")


def type_body(name="Самосвал"):
    return {
        "name": name,
        "requires_sts": True,
        "requires_calibration": True,
        "requires_wialon": True,
    }


def vehicle_body(**values):
    result = {
        "name": "Truck",
        "plate_number": "A123BC86",
        "type_id": None,
        "accounting_state": "Действующая",
        "model": None,
        "wialon_unit_id": None,
    }
    result.update(values)
    return result


def test_arm2_routes_auth_permissions_csrf_and_inactive_user(client, users, db):
    assert client.get("/api/arm2/types").status_code == 401
    assert client.post("/api/arm2/types", json=type_body()).status_code == 401

    set_operator(db, [Permission.ARM2_TYPE_VIEW])
    sign_in(client)
    assert client.get("/api/arm2/types").status_code == 200
    assert client.post("/api/arm2/types", json=type_body()).status_code == 403
    assert client.get("/api/arm2/vehicles").status_code == 403

    set_operator(db, [Permission.ARM2_VEH_VIEW, Permission.ARM2_VEH_EDIT])
    assert client.get("/api/arm2/vehicles").status_code == 200
    assert client.get("/api/arm2/types").status_code == 403
    response = client.post(
        "/api/arm2/vehicles",
        json=vehicle_body(),
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(Vehicle)) == 0

    set_operator(
        db,
        [Permission.ARM2_VEH_VIEW, Permission.ARM2_VEH_EDIT],
        active=False,
    )
    assert client.get("/api/arm2/vehicles").status_code == 401
    assert client.post("/api/arm2/vehicles", json=vehicle_body()).status_code == 401
    assert db.scalar(select(func.count()).select_from(Vehicle)) == 0


def test_type_http_lifecycle_validation_history_and_pagination(client, users, db):
    operator = set_operator(
        db, [Permission.ARM2_TYPE_VIEW, Permission.ARM2_TYPE_EDIT]
    )
    sign_in(client)

    response = client.post(
        "/api/arm2/types",
        json={"name": "  Самосвал  ", "requires_calibration": None},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["id"].startswith("TYPE-") and created["id"][5:].isdigit()
    assert {key: value for key, value in created.items() if key != "id"} == {
        "name": "Самосвал",
        "requires_sts": True,
        "requires_calibration": None,
        "requires_wialon": None,
        "status": "REVIEW",
        "version": 1,
    }
    assert client.get(f"/api/arm2/types/{created['id']}").json() == created

    confirmed_body = {"expected_version": 1, **type_body("Самосвал")}
    confirmed = client.put(
        f"/api/arm2/types/{created['id']}", json=confirmed_body
    )
    assert confirmed.status_code == 200
    assert (confirmed.json()["status"], confirmed.json()["version"]) == (
        "CONFIRMED",
        2,
    )
    no_op = client.put(
        f"/api/arm2/types/{created['id']}",
        json={**confirmed_body, "expected_version": 2},
    )
    assert no_op.status_code == 200 and no_op.json()["version"] == 2

    reviewed = client.put(
        f"/api/arm2/types/{created['id']}",
        json={**confirmed_body, "expected_version": 2, "requires_wialon": None},
    )
    assert reviewed.status_code == 200
    assert (reviewed.json()["status"], reviewed.json()["version"]) == ("REVIEW", 3)
    assert client.put(
        f"/api/arm2/types/{created['id']}", json=confirmed_body
    ).status_code == 409
    duplicate = client.post("/api/arm2/types", json=type_body("Самосвал"))
    assert duplicate.status_code == 409
    assert "sql" not in duplicate.text.lower()
    assert client.post(
        "/api/arm2/types", json={**type_body("Invalid STS"), "requires_sts": False}
    ).status_code == 422

    for forbidden in ("id", "status", "version", "actor_id", "source"):
        assert client.post(
            "/api/arm2/types", json={**type_body("Extra"), forbidden: "client"}
        ).status_code == 422
    missing_version = dict(type_body("Missing"))
    assert client.put(
        f"/api/arm2/types/{created['id']}", json=missing_version
    ).status_code == 422
    assert client.put(
        f"/api/arm2/types/{created['id']}",
        json={"expected_version": "3", **type_body("Strict")},
    ).status_code == 422
    assert client.get("/api/arm2/types/TYPE-999999").status_code == 404

    second = client.post("/api/arm2/types", json=type_body("Loader")).json()
    page = client.get("/api/arm2/types?limit=1&offset=1")
    assert page.status_code == 200 and page.json() == [
        client.get(f"/api/arm2/types/{second['id']}").json()
    ]
    assert client.get("/api/arm2/types?limit=0").status_code == 422
    assert client.get("/api/arm2/types?offset=-1").status_code == 422

    statements = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert client.get("/api/arm2/types").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("arm2_history_events" in statement for statement in statements)

    events = db.scalars(
        select(HistoryEvent)
        .where(HistoryEvent.object_id == created["id"])
        .order_by(HistoryEvent.event_id)
    ).all()
    assert len(events) == 3
    assert all(
        (item.actor_kind, item.actor_id, item.source)
        == ("HUMAN", str(operator.id), "arm2-api")
        for item in events
    )


def test_vehicle_http_type_owner_and_history_contract(client, users, db):
    operator = set_operator(
        db,
        [
            Permission.ARM2_TYPE_VIEW,
            Permission.ARM2_TYPE_EDIT,
            Permission.ARM2_VEH_VIEW,
            Permission.ARM2_VEH_EDIT,
        ],
    )
    sign_in(client)
    confirmed_type = client.post("/api/arm2/types", json=type_body()).json()
    review_type = client.post(
        "/api/arm2/types", json={"name": "Incomplete"}
    ).json()

    admin = db.scalar(select(User).where(User.login == "admin"))
    cp = create_counterparty(
        db,
        "CP",
        full_name="CP full",
        legal_address="Address",
        principal=admin,
        context=server_context(admin),
    )
    org = create_organization(
        db, "ORG", principal=admin, context=server_context(admin)
    )
    db.commit()

    response = client.post(
        "/api/arm2/vehicles",
        json=vehicle_body(type_id=confirmed_type["id"], owner_cp_id=cp.id),
    )
    assert response.status_code == 201, response.text
    vehicle = response.json()
    vehicle_id = vehicle["id"]
    assert vehicle["plate_number"] == "А123ВС86"
    assert (vehicle["owner_cp_id"], vehicle["owner_org_id"], vehicle["version"]) == (
        cp.id,
        None,
        1,
    )
    assert set(vehicle) == {
        "id", "name", "plate_number", "type_id", "accounting_state", "model",
        "wialon_unit_id", "owner_cp_id", "owner_org_id", "version",
    }
    duplicate = client.post(
        "/api/arm2/vehicles",
        json=vehicle_body(name="Duplicate", type_id=confirmed_type["id"]),
    )
    assert duplicate.status_code == 409
    assert client.post(
        "/api/arm2/vehicles",
        json=vehicle_body(
            name="Bad type", plate_number="B123BC86", type_id=review_type["id"]
        ),
    ).status_code == 422

    update = {
        "expected_version": 1,
        **vehicle_body(
            name="Truck updated",
            plate_number=vehicle["plate_number"],
            type_id=confirmed_type["id"],
            model="Model 1",
        ),
    }
    vehicle = client.put(f"/api/arm2/vehicles/{vehicle_id}", json=update).json()
    assert (vehicle["version"], vehicle["owner_cp_id"]) == (2, cp.id)
    assert client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 2, "owner_org_id": org.id},
    ).status_code == 422
    vehicle = client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={
            **update,
            "expected_version": 2,
            "owner_cp_id": None,
            "owner_org_id": org.id,
        },
    ).json()
    assert (vehicle["version"], vehicle["owner_cp_id"], vehicle["owner_org_id"]) == (
        3,
        None,
        org.id,
    )

    type_review = client.put(
        f"/api/arm2/types/{confirmed_type['id']}",
        json={
            "expected_version": 1,
            **type_body(),
            "requires_calibration": None,
        },
    )
    assert type_review.status_code == 200 and type_review.json()["status"] == "REVIEW"
    vehicle = client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 3, "name": "Same TYPE remains"},
    ).json()
    assert (vehicle["version"], vehicle["type_id"], vehicle["owner_org_id"]) == (
        4,
        confirmed_type["id"],
        org.id,
    )
    vehicle = client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 4, "name": vehicle["name"], "owner_org_id": None},
    ).json()
    assert (vehicle["version"], vehicle["owner_cp_id"], vehicle["owner_org_id"]) == (
        5,
        None,
        None,
    )
    vehicle = client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 5, "name": vehicle["name"], "owner_cp_id": cp.id},
    ).json()
    assert vehicle["version"] == 6 and vehicle["owner_cp_id"] == cp.id

    update_counterparty(
        db,
        cp.id,
        1,
        cp.name,
        full_name=None,
        principal=admin,
        context=server_context(admin),
    )
    db.commit()
    vehicle = client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 6, "name": "Owner remains in REVIEW"},
    ).json()
    assert vehicle["version"] == 7 and vehicle["owner_cp_id"] == cp.id
    assert client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={
            **update,
            "expected_version": 7,
            "owner_cp_id": cp.id,
            "owner_org_id": org.id,
        },
    ).status_code == 422
    assert client.put(
        f"/api/arm2/vehicles/{vehicle_id}",
        json={**update, "expected_version": 7, "accounting_state": "Unknown"},
    ).status_code == 422
    assert client.get("/api/arm2/vehicles/VEH-999999").status_code == 404
    assert client.get(f"/api/arm2/vehicles/{vehicle_id}").json()["owner_cp_id"] == cp.id

    events = db.scalars(
        select(HistoryEvent)
        .where(HistoryEvent.object_id == vehicle_id)
        .order_by(HistoryEvent.event_id)
    ).all()
    assert len(events) == 7
    assert all(item.actor_id == str(operator.id) for item in events)
    assert events[-1].related_objects[-1] == {
        "object_type": "CP",
        "object_id": cp.id,
        "relation": "operational_owner",
    }


def test_unexpected_history_error_is_atomic_over_http(users, db, monkeypatch):
    set_operator(db, [Permission.ARM2_TYPE_VIEW, Permission.ARM2_TYPE_EDIT])

    def broken_history(*_args, **_kwargs):
        raise RuntimeError("history unavailable")

    monkeypatch.setitem(create_object.__kwdefaults__, "history_writer", broken_history)
    with TestClient(app, raise_server_exceptions=False) as browser:
        sign_in(browser)
        response = browser.post("/api/arm2/types", json=type_body("Atomic"))
        assert response.status_code == 500
        assert "history unavailable" not in response.text

    db.expire_all()
    assert db.scalar(
        select(func.count()).select_from(VehicleType).where(VehicleType.name == "Atomic")
    ) == 0
    assert db.scalar(
        select(func.count()).select_from(HistoryEvent).where(
            HistoryEvent.object_type == "TYPE"
        )
    ) == 0


def test_save_authorization_failure_returns_403_and_is_atomic(
    client, users, db, monkeypatch
):
    operator = set_operator(
        db, [Permission.ARM2_TYPE_VIEW, Permission.ARM2_TYPE_EDIT]
    )
    sign_in(client)
    type_count = db.scalar(select(func.count()).select_from(VehicleType))
    history_count = db.scalar(select(func.count()).select_from(HistoryEvent))
    create_vehicle_type = arm2_routes.create_vehicle_type

    def revoke_edit_permission(
        db_session, *args, principal, context, **kwargs
    ):
        principal.permissions = [Permission.ARM2_TYPE_VIEW]
        db_session.flush()
        return create_vehicle_type(
            db_session,
            *args,
            principal=principal,
            context=context,
            **kwargs,
        )

    monkeypatch.setattr(
        arm2_routes, "create_vehicle_type", revoke_edit_permission
    )
    response = client.post("/api/arm2/types", json=type_body("Forbidden"))

    assert response.status_code == 403
    assert response.json() == {"detail": "ARM2 write permission denied"}
    assert "Permission required" not in response.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(VehicleType)) == type_count
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == history_count
    assert db.get(User, operator.id).permissions == [
        Permission.ARM2_TYPE_VIEW,
        Permission.ARM2_TYPE_EDIT,
    ]
