from sqlalchemy import func, select

from app.arm2 import routes as arm2_routes
from app.arm2.counterparty_models import Counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.organization_models import Organization
from app.arm2.representative_models import (
    CounterpartyRepresentative,
    OrganizationRepresentative,
)
from app.models import User
from app.permissions import Permission


def set_operator(db, permissions):
    operator = db.scalar(select(User).where(User.login == "operator"))
    operator.permissions = permissions
    db.commit()
    return operator


def sign_in(client):
    response = client.post(
        "/api/auth/login",
        json={"login": "operator", "password": "test-password-123"},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["csrf"]


def party_body(name, **values):
    result = {"name": name}
    result.update(values)
    return result


def representative_update(record, **values):
    result = {
        "expected_version": record["version"],
        "name": record["name"],
        "position": record["position"],
        "authority_basis": record["authority_basis"],
        "phone": record["phone"],
        "email": record["email"],
        "state": record["state"],
        "user_id": record["user_id"],
        "last_name": record["last_name"],
        "first_name": record["first_name"],
        "middle_name": record["middle_name"],
    }
    result.update(values)
    return result


def all_party_permissions():
    return [
        Permission.ARM2_CP_VIEW,
        Permission.ARM2_CP_EDIT,
        Permission.ARM2_ORG_VIEW,
        Permission.ARM2_ORG_EDIT,
    ]


def test_party_routes_auth_permissions_and_csrf(client, users, db):
    assert client.get("/api/arm2/counterparties").status_code == 401
    assert client.post(
        "/api/arm2/counterparties", json=party_body("CP")
    ).status_code == 401

    set_operator(db, [Permission.ARM2_CP_VIEW])
    sign_in(client)
    assert client.get("/api/arm2/counterparties").status_code == 200
    assert client.post(
        "/api/arm2/counterparties", json=party_body("CP")
    ).status_code == 403
    assert client.get("/api/arm2/organizations").status_code == 403

    set_operator(db, [Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT])
    response = client.post(
        "/api/arm2/counterparties",
        json=party_body("No CSRF"),
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(Counterparty)) == 0

    set_operator(db, [Permission.ARM2_ORG_VIEW, Permission.ARM2_ORG_EDIT])
    assert client.get("/api/arm2/organizations").status_code == 200
    assert client.get("/api/arm2/counterparties").status_code == 403
    assert client.get(
        "/api/arm2/counterparties/CP-999999/representatives"
    ).status_code == 403
    assert client.get(
        "/api/arm2/organizations/ORG-999999/representatives"
    ).status_code == 404


def test_party_lifecycle_partial_fields_status_uniqueness_and_history(
    client, users, db
):
    operator = set_operator(db, all_party_permissions())
    sign_in(client)
    cp_body = party_body(
        "CP One",
        full_name="Counterparty Full",
        inn="0012345678",
        bank_name="Bank",
        bik="000000001",
        settlement_account="00000000000000000001",
        correspondent_account="00000000000000000002",
    )
    response = client.post("/api/arm2/counterparties", json=cp_body)
    assert response.status_code == 201, response.text
    cp = response.json()
    assert cp["status"] == "REVIEW" and cp["version"] == 1
    assert cp["inn"] == "0012345678"
    assert set(cp) == {
        "id",
        "name",
        "full_name",
        "legal_address",
        "postal_address",
        "legal_form",
        "short_name",
        "inn",
        "kpp",
        "ogrn",
        "phone",
        "email",
        "bank_name",
        "bik",
        "settlement_account",
        "correspondent_account",
        "primary_signer_id",
        "status",
        "version",
    }

    confirmed = client.put(
        f"/api/arm2/counterparties/{cp['id']}",
        json={
            "expected_version": 1,
            "name": "CP One",
            "legal_address": "Legal address",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    cp = confirmed.json()
    assert (cp["status"], cp["version"]) == ("CONFIRMED", 2)
    assert cp["full_name"] == "Counterparty Full"
    assert cp["bank_name"] == "Bank"
    assert cp["settlement_account"] == "00000000000000000001"

    no_op = client.put(
        f"/api/arm2/counterparties/{cp['id']}",
        json={"expected_version": 2, "name": "CP One"},
    )
    assert no_op.status_code == 200 and no_op.json()["version"] == 2
    cleared = client.put(
        f"/api/arm2/counterparties/{cp['id']}",
        json={"expected_version": 2, "name": "CP One", "bank_name": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["bank_name"] is None
    assert cleared.json()["legal_address"] == "Legal address"
    assert cleared.json()["version"] == 3
    before_conflict_history = db.scalar(
        select(func.count()).select_from(HistoryEvent)
    )
    stale = client.put(
        f"/api/arm2/counterparties/{cp['id']}",
        json={"expected_version": 2, "name": "Stale"},
    )
    assert stale.status_code == 409
    assert db.scalar(
        select(func.count()).select_from(HistoryEvent)
    ) == before_conflict_history
    reviewed = client.put(
        f"/api/arm2/counterparties/{cp['id']}",
        json={
            "expected_version": 3,
            "name": "CP One",
            "legal_address": None,
        },
    )
    assert reviewed.status_code == 200
    assert (reviewed.json()["status"], reviewed.json()["version"]) == (
        "REVIEW",
        4,
    )

    org = client.post(
        "/api/arm2/organizations",
        json=party_body(
            "ORG One",
            full_name="Organization Full",
            legal_address="Organization address",
            inn="0012345678",
        ),
    )
    assert org.status_code == 201, org.text
    org = org.json()
    assert org["status"] == "CONFIRMED"
    assert org["inn"] == cp["inn"]
    org_update = client.put(
        f"/api/arm2/organizations/{org['id']}",
        json={
            "expected_version": 1,
            "name": "ORG Updated",
            "postal_address": "Postal address",
        },
    )
    assert org_update.status_code == 200
    org = org_update.json()
    assert org["name"] == "ORG Updated" and org["version"] == 2
    assert org["legal_address"] == "Organization address"
    assert client.get(f"/api/arm2/organizations/{org['id']}").json() == org
    assert client.get("/api/arm2/organizations?limit=1&offset=0").json() == [org]

    before_cp = db.scalar(select(func.count()).select_from(Counterparty))
    before_history = db.scalar(select(func.count()).select_from(HistoryEvent))
    duplicate = client.post(
        "/api/arm2/counterparties",
        json=party_body("Duplicate", inn="0012345678"),
    )
    assert duplicate.status_code == 409
    assert "sql" not in duplicate.text.lower()
    assert db.scalar(select(func.count()).select_from(Counterparty)) == before_cp
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before_history

    for invalid in (
        party_body("Bad email", email="not-an-email"),
        party_body("Bad BIK", bik="123"),
    ):
        response = client.post("/api/arm2/counterparties", json=invalid)
        assert response.status_code == 422
    for forbidden in (
        "id",
        "status",
        "version",
        "actor_id",
        "source",
        "primary_signer_id",
    ):
        response = client.post(
            "/api/arm2/counterparties",
            json={"name": "Spoofed", forbidden: "client"},
        )
        assert response.status_code == 422

    second = client.post(
        "/api/arm2/counterparties", json=party_body("CP Two")
    ).json()
    page = client.get("/api/arm2/counterparties?limit=1&offset=1")
    assert page.status_code == 200 and page.json() == [second]
    assert client.get("/api/arm2/counterparties?limit=0").status_code == 422
    assert client.get("/api/arm2/organizations?offset=-1").status_code == 422
    assert client.get(f"/api/arm2/counterparties/{cp['id']}").status_code == 200
    assert client.get("/api/arm2/counterparties/CP-999999").status_code == 404

    events = db.scalars(
        select(HistoryEvent)
        .where(HistoryEvent.object_id == cp["id"])
        .order_by(HistoryEvent.event_id)
    ).all()
    assert len(events) == 4
    assert all(
        (event.actor_kind, event.actor_id, event.source)
        == ("HUMAN", str(operator.id), "arm2-api")
        for event in events
    )


def test_representatives_parent_scope_full_put_and_primary_signer(client, users, db):
    operator = set_operator(db, all_party_permissions())
    sign_in(client)
    cp_one = client.post(
        "/api/arm2/counterparties", json=party_body("CP One")
    ).json()
    cp_two = client.post(
        "/api/arm2/counterparties", json=party_body("CP Two")
    ).json()
    org = client.post(
        "/api/arm2/organizations", json=party_body("ORG One")
    ).json()

    response = client.post(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives",
        json={
            "name": "Ivanov Ivan",
            "position": "Director",
            "email": "ivanov@example.test",
            "last_name": "Ivanov",
            "first_name": "Ivan",
        },
    )
    assert response.status_code == 201, response.text
    cp_rep = response.json()
    assert cp_rep["parent_id"] == cp_one["id"]
    assert cp_rep["state"] == "Действующий" and cp_rep["version"] == 1
    assert client.get(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives/{cp_rep['id']}"
    ).json() == cp_rep

    spoofed = client.post(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives",
        json={"name": "Spoofed", "parent_id": cp_two["id"]},
    )
    assert spoofed.status_code == 422
    for forbidden in ("id", "status", "version", "actor_id", "role", "permissions"):
        body = {"name": "Spoofed", forbidden: "client"}
        assert client.post(
            f"/api/arm2/counterparties/{cp_one['id']}/representatives",
            json=body,
        ).status_code == 422
    missing_field = client.put(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives/{cp_rep['id']}",
        json={"expected_version": 1, "name": "Incomplete"},
    )
    assert missing_field.status_code == 422
    unchanged = client.get(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives/{cp_rep['id']}"
    ).json()
    assert unchanged == cp_rep

    updated = client.put(
        f"/api/arm2/counterparties/{cp_one['id']}/representatives/{cp_rep['id']}",
        json=representative_update(cp_rep, position="General Director"),
    )
    assert updated.status_code == 200, updated.text
    cp_rep = updated.json()
    assert cp_rep["position"] == "General Director" and cp_rep["version"] == 2

    assert client.get(
        f"/api/arm2/counterparties/{cp_two['id']}/representatives/{cp_rep['id']}"
    ).status_code == 404
    wrong_parent_put = client.put(
        f"/api/arm2/counterparties/{cp_two['id']}/representatives/{cp_rep['id']}",
        json=representative_update(cp_rep),
    )
    assert wrong_parent_put.status_code == 404

    signer = client.put(
        f"/api/arm2/counterparties/{cp_one['id']}",
        json={
            "expected_version": cp_one["version"],
            "name": cp_one["name"],
            "primary_signer_id": cp_rep["id"],
        },
    )
    assert signer.status_code == 200, signer.text
    cp_one = signer.json()
    assert cp_one["primary_signer_id"] == cp_rep["id"]

    other_rep = client.post(
        f"/api/arm2/counterparties/{cp_two['id']}/representatives",
        json={"name": "Other representative"},
    ).json()
    invalid_signer = client.put(
        f"/api/arm2/counterparties/{cp_one['id']}",
        json={
            "expected_version": cp_one["version"],
            "name": cp_one["name"],
            "primary_signer_id": other_rep["id"],
        },
    )
    assert invalid_signer.status_code == 422
    assert client.get(f"/api/arm2/counterparties/{cp_one['id']}").json()[
        "primary_signer_id"
    ] == cp_rep["id"]

    org_rep_response = client.post(
        f"/api/arm2/organizations/{org['id']}/representatives",
        json={"name": "Organization signer", "state": "Архивный"},
    )
    assert org_rep_response.status_code == 201, org_rep_response.text
    org_rep = org_rep_response.json()
    assert org_rep["parent_id"] == org["id"]
    assert org_rep["state"] == "Архивный"
    org_signer = client.put(
        f"/api/arm2/organizations/{org['id']}",
        json={
            "expected_version": org["version"],
            "name": org["name"],
            "primary_signer_id": org_rep["id"],
        },
    )
    assert org_signer.status_code == 200
    assert org_signer.json()["primary_signer_id"] == org_rep["id"]
    assert client.get(
        f"/api/arm2/organizations/{org['id']}/representatives?limit=1&offset=0"
    ).json() == [org_rep]
    assert client.post(
        "/api/arm2/organizations/ORG-999999/representatives",
        json={"name": "Missing parent"},
    ).status_code == 404
    representative_events = db.scalars(
        select(HistoryEvent)
        .where(HistoryEvent.object_id == cp_rep["id"])
        .order_by(HistoryEvent.event_id)
    ).all()
    assert len(representative_events) == 2
    assert all(
        (event.actor_kind, event.actor_id, event.source)
        == ("HUMAN", str(operator.id), "arm2-api")
        for event in representative_events
    )


def test_representative_validation_and_user_link_do_not_grant_access(
    client, users, db
):
    operator = set_operator(db, all_party_permissions())
    sign_in(client)
    cp = client.post(
        "/api/arm2/counterparties", json=party_body("CP")
    ).json()
    response = client.post(
        f"/api/arm2/counterparties/{cp['id']}/representatives",
        json={
            "name": "Linked operator",
            "user_id": operator.id,
            "email": "linked@example.test",
        },
    )
    assert response.status_code == 201, response.text
    representative = response.json()
    assert representative["user_id"] == operator.id

    set_operator(db, [Permission.ARM2_ORG_VIEW])
    assert client.get(
        f"/api/arm2/counterparties/{cp['id']}/representatives"
    ).status_code == 403
    assert client.get("/api/arm2/counterparties").status_code == 403

    set_operator(db, [Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT])
    before = db.scalar(
        select(func.count()).select_from(CounterpartyRepresentative)
    )
    before_history = db.scalar(select(func.count()).select_from(HistoryEvent))
    invalid = client.post(
        f"/api/arm2/counterparties/{cp['id']}/representatives",
        json={"name": "Invalid", "email": "bad-email"},
    )
    assert invalid.status_code == 422
    assert db.scalar(
        select(func.count()).select_from(CounterpartyRepresentative)
    ) == before
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before_history


def test_party_save_authorization_failure_returns_403_and_rolls_back(
    client, users, db, monkeypatch
):
    operator = set_operator(
        db, [Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT]
    )
    sign_in(client)
    before_history = db.scalar(select(func.count()).select_from(HistoryEvent))
    create_counterparty = arm2_routes.create_counterparty

    def revoke_edit_permission(
        db_session, *args, principal, context, **kwargs
    ):
        principal.permissions = [Permission.ARM2_CP_VIEW]
        db_session.flush()
        return create_counterparty(
            db_session,
            *args,
            principal=principal,
            context=context,
            **kwargs,
        )

    monkeypatch.setattr(
        arm2_routes, "create_counterparty", revoke_edit_permission
    )
    response = client.post(
        "/api/arm2/counterparties", json=party_body("Forbidden")
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "ARM2 write permission denied"}
    assert db.scalar(select(func.count()).select_from(Counterparty)) == 0
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before_history
    db.expire_all()
    assert db.get(User, operator.id).permissions == [
        Permission.ARM2_CP_VIEW,
        Permission.ARM2_CP_EDIT,
    ]
    assert db.scalar(select(func.count()).select_from(Organization)) == 0
    assert db.scalar(
        select(func.count()).select_from(OrganizationRepresentative)
    ) == 0
