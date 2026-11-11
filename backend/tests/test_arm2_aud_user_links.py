import pytest
from sqlalchemy import func, select
from test_arm2_party_routes import representative_update

from app.arm2.history_models import HistoryEvent
from app.arm2.representative_models import CounterpartyRepresentative, OrganizationRepresentative
from app.models import User


@pytest.mark.parametrize("path,kind,model", [
    ("counterparties", "CPREP", CounterpartyRepresentative),
    ("organizations", "ORGREP", OrganizationRepresentative),
])
def test_user_link_lifecycle_persisted_record_history_and_unchanged_permissions(admin, db, path, kind, model):
    accounts = admin.get("/api/users")
    assert accounts.status_code == 200
    first, second = accounts.json()[:2]
    snapshots = {user.id: (user.role, list(user.permissions or [])) for user in db.scalars(select(User))}
    parent = admin.post(f"/api/arm2/{path}", json={"name": "Party"}).json()
    collection = f"/api/arm2/{path}/{parent['id']}/representatives"
    response = admin.post(collection, json={"name": "Linked representative", "user_id": first["id"]})
    assert response.status_code == 201, response.text
    rep = response.json()
    resource = f"{collection}/{rep['id']}"
    expected_links = [first["id"], second["id"], None]
    for version, expected in enumerate(expected_links, 1):
        if version > 1:
            response = admin.put(resource, json=representative_update(rep, user_id=expected))
            assert response.status_code == 200, response.text
        rep = admin.get(resource).json()
        db.expire_all()
        stored = db.get(model, rep["id"])
        assert (stored.user_id, stored.version) == (expected, version)
        assert (rep["user_id"], rep["version"]) == (expected, version)
        assert expected is None or type(rep["user_id"]) is int
        event = db.scalar(select(HistoryEvent).where(
            HistoryEvent.object_type == kind, HistoryEvent.object_id == rep["id"],
            HistoryEvent.version_after == version,
        ))
        assert event.changes["user_id"] == {"before": expected_links[version - 2] if version > 1 else None,
                                            "after": expected}
        detail = admin.get(f"/api/arm2/history/events/{event.event_id}")
        assert detail.status_code == 200
        assert detail.json()["changes"]["user_id"] == event.changes["user_id"]
    count = db.scalar(select(func.count()).select_from(HistoryEvent))
    no_op = admin.put(resource, json=representative_update(rep))
    assert no_op.status_code == 200 and no_op.json()["version"] == 3
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == count
    assert admin.put(resource, json=representative_update(rep, user_id=999999)).status_code == 422
    assert admin.put(resource, json=representative_update(rep, expected_version=1,
                                                        user_id=first["id"])).status_code == 409
    db.expire_all()
    assert (db.get(model, rep["id"]).user_id, db.get(model, rep["id"]).version) == (None, 3)
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == count
    assert {user.id: (user.role, list(user.permissions or [])) for user in db.scalars(select(User))} == snapshots


def test_arm2_edit_does_not_grant_access_to_user_directory(client, users, db):
    operator = db.scalar(select(User).where(User.login == "operator"))
    operator.permissions = ["arm2_cp_view", "arm2_cp_edit", "arm2_org_view", "arm2_org_edit"]
    db.commit()
    assert client.post("/api/auth/login", json={"login": "operator", "password": "test-password-123"}).status_code == 200
    assert client.get("/api/users").status_code == 403
