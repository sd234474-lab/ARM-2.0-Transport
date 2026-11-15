import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.arm2.document_kind_models import DocumentKind
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext
from app.arm2.vehicle_type_store import create_vehicle_type
from app.auth import hasher
from app.config import settings
from app.db import engine
from app.models import User

PATH = "/api/arm2/document-kinds"
VIEW, EDIT = "arm2_doc_kind_view", "arm2_doc_kind_edit"


@pytest.fixture
def api(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()
    from app.api import app

    try:
        with Session(engine(), expire_on_commit=False) as db:
            user = User(login="kind-api", name="kind-api", password_hash=hasher.hash("doc-kind-password-123"),
                        role="OPERATOR", active=True, permissions=[VIEW, EDIT])
            db.add(user)
            db.commit()
            with TestClient(app) as client:
                response = client.post("/api/auth/login", json={"login": user.login, "password": "doc-kind-password-123"})
                assert response.status_code == 200
                client.headers["X-CSRF-Token"] = client.cookies["csrf"]
                yield client, db, user
    finally:
        if engine.cache_info().currsize:
            engine().dispose()
        engine.cache_clear()
        settings.cache_clear()


def body(**changes):
    return {"name": "СТС", "applicable_object_types": ["VEH"], "max_current_documents": 1} | changes


def post(client, values=None):
    response = client.post(PATH, json=body() if values is None else values)
    assert response.status_code == 201, response.text
    return response.json()


def counts(db):
    db.expire_all()
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (DocumentKind, HistoryEvent))


def test_api_create_commit_read_update_noop_conflict_and_history(api):
    client, db, user = api
    created = post(client)
    assert created == {"id": "DOC-KIND-000001", **body(), "version": 1}
    id = created["id"]
    assert client.get(PATH + "/" + id).json() == created
    assert client.get(PATH).json() == [created]
    assert counts(db) == (1, 1)
    persisted = db.get(DocumentKind, id)
    assert persisted.version == 1
    event = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == id))
    assert (event.operation, event.actor_kind, event.actor_id, event.source, event.version_after) == (
        "CREATED", "HUMAN", str(user.id), "arm2-api", 1,
    )
    history = f"/api/arm2/history/DOC-KIND/{id}"
    summary = client.get(history)
    assert summary.status_code == 200 and len(summary.json()) == 1
    detail = client.get(f"/api/arm2/history/events/{event.event_id}")
    assert detail.status_code == 200
    assert detail.json()["object_type"] == "DOC-KIND"
    assert detail.json()["changes"] == {
        "name": {"before": None, "after": "СТС"}, "version": {"before": None, "after": 1},
        "applicable_object_types": {"before": None, "after": ["VEH"]},
        "max_current_documents": {"before": None, "after": 1},
    }
    changed_body = body(name="Доверенность", applicable_object_types=["ORG", "CP"], max_current_documents=None)
    response = client.put(PATH + "/" + id, json={"expected_version": 1, **changed_body})
    assert response.status_code == 200
    changed = {"id": id, **changed_body, "applicable_object_types": ["CP", "ORG"], "version": 2}
    assert response.json() == changed
    assert client.get(PATH + "/" + id).json() == changed
    assert counts(db) == (1, 2)
    updated = client.get(history).json()[0]
    diff = client.get(f"/api/arm2/history/events/{updated['event_id']}").json()["changes"]
    assert diff == {
        "name": {"before": "СТС", "after": "Доверенность"},
        "applicable_object_types": {"before": ["VEH"], "after": ["CP", "ORG"]},
        "max_current_documents": {"before": 1, "after": None}, "version": {"before": 1, "after": 2},
    }
    for parents in [["ORG", "CP"], ["CP", "ORG"]]:
        response = client.put(PATH + "/" + id, json={"expected_version": 2, **changed_body, "applicable_object_types": parents})
        assert response.status_code == 200 and response.json() == changed
        assert counts(db) == (1, 2)
    assert client.put(PATH + "/" + id, json={"expected_version": 1, **body()}).status_code == 409
    assert counts(db) == (1, 2)


def test_api_rejects_bad_bodies_for_post_and_put_without_changes(api):
    client, db, _ = api
    created = post(client)
    invalid = [body(name=""), body(name="  "), body(name=True), body(applicable_object_types=[]),
               body(applicable_object_types=["TYPE"]), body(applicable_object_types=["CP", "CP"]),
               body(applicable_object_types="CP"), body(applicable_object_types=[1]), body(applicable_object_types=None),
               body(applicable_object_types=[" CP "]), body(applicable_object_types=["cp"])]
    invalid.extend(body(max_current_documents=value) for value in [True, False, "1", 0, -1, 1.5])
    invalid.extend({key: value for key, value in body().items() if key != missing} for missing in body())
    invalid.extend(body(**{key: value}) for key, value in [
        ("id", "DOC-KIND-999999"), ("version", 999), ("revision_policy", "fake"),
        ("file_naming_template", "fake"), ("actor_id", "999999"), ("source", "spoofed"),
    ])
    for values in invalid:
        assert client.post(PATH, json=values).status_code == 422, values
        assert client.put(PATH + "/" + created["id"], json={"expected_version": 1, **values}).status_code == 422, values
        assert counts(db) == (1, 1)
        assert client.get(PATH + "/" + created["id"]).json() == created
    for version in [True, False, 0, -1, "1", 1.5, None]:
        assert client.put(PATH + "/" + created["id"], json={**body(), "expected_version": version}).status_code == 422
    assert client.put(PATH + "/" + created["id"], json=body()).status_code == 422
    assert client.post(PATH, json={**body(), "expected_version": 1}).status_code == 422
    assert counts(db) == (1, 1)


def test_api_duplicate_name_returns_409_and_rolls_back_data_and_history(api):
    client, db, _ = api
    first = post(client)
    second = post(client, body(name="Другой"))
    assert client.post(PATH, json=body(name=" СТС ")).status_code == 409
    assert client.put(PATH + "/" + second["id"], json={"expected_version": 1, **body()}).status_code == 409
    assert client.get(PATH + "/" + first["id"]).json() == first
    assert client.get(PATH + "/" + second["id"]).json() == second
    assert counts(db) == (2, 2)
    response = client.put(PATH + "/" + second["id"], json={"expected_version": 1, **body(name="Восстановлено")})
    assert response.status_code == 200 and response.json()["version"] == 2
    assert counts(db) == (2, 3)


def test_api_view_edit_prerequisite_and_history_acl(api):
    client, db, user = api
    record = post(client)
    event_id = db.scalar(select(HistoryEvent.event_id).where(HistoryEvent.object_id == record["id"]))
    paths = [PATH, PATH + "/" + record["id"], "/api/arm2/history/DOC-KIND/" + record["id"],
             f"/api/arm2/history/events/{event_id}"]
    for permissions in [[], [VIEW], [EDIT], ["arm2_type_view", "arm2_type_edit"], [VIEW, EDIT]]:
        user.permissions = permissions
        db.commit()
        for resource in paths:
            assert client.get(resource).status_code == (200 if VIEW in permissions else 403), (permissions, resource)
        if permissions != [VIEW, EDIT]:
            assert client.post(PATH, json=body(name="Denied")).status_code == 403
            assert client.put(PATH + "/" + record["id"], json={"expected_version": 1, **body(name="Denied")}).status_code == 403
            assert counts(db) == (1, 1)
    # Real auth/session/CSRF machinery, no dependency overrides.
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(PATH, json=body(name="CSRF denied")).status_code == 403
    client.headers["X-CSRF-Token"] = token
    client.cookies.clear()
    for resource in paths:
        assert client.get(resource).status_code == 401
    assert client.post(PATH, json=body(name="Anonymous")).status_code == 401
    assert client.put(PATH + "/" + record["id"], json={"expected_version": 1, **body()}).status_code == 401
    assert counts(db) == (1, 1)


def test_api_list_order_pagination_and_not_found(api):
    client, db, _ = api
    records = [post(client, body(name=f"Тип {105 - index:03}")) for index in range(105)]
    ids = [record["id"] for record in records]
    assert ids == sorted(ids)
    assert [row["id"] for row in client.get(PATH).json()] == ids[:100]
    assert [row["id"] for row in client.get(PATH + "?offset=100").json()] == ids[100:]
    assert [row["id"] for row in client.get(PATH + "?limit=2&offset=98").json()] == ids[98:100]
    assert client.get(PATH + "?offset=105").json() == []
    for params in ["limit=0", "limit=101", "offset=-1", "limit=1.5", "offset=bad"]:
        assert client.get(PATH + "?" + params).status_code == 422
    assert client.get(PATH + "/DOC-KIND-999999").status_code == 404
    assert client.put(PATH + "/DOC-KIND-999999", json={"expected_version": 1, **body()}).status_code == 404
    for invalid_id in ["TYPE-000001", "bad", "DOC-KIND-1"]:
        assert client.get(PATH + "/" + invalid_id).status_code == 404
        assert client.put(PATH + "/" + invalid_id, json={"expected_version": 1, **body()}).status_code == 422
    assert counts(db) == (105, 105)


def test_api_history_pagination_and_no_cross_resource_substitution(api):
    client, db, _ = api
    record = post(client)
    for version in range(1, 5):
        response = client.put(PATH + "/" + record["id"], json={"expected_version": version, **body(name=f"Изменение {version}")})
        assert response.status_code == 200
    history = "/api/arm2/history/DOC-KIND/" + record["id"]
    first = client.get(history + "?limit=2").json()
    next_page = client.get(history + "?limit=2&offset=2").json()
    assert [row["version_after"] for row in first + next_page] == [5, 4, 3, 2]
    assert client.get(history + "?offset=4").json()[0]["operation"] == "CREATED"
    for query in ["limit=101", "limit=0", "offset=-1"]:
        assert client.get(history + "?" + query).status_code == 422
    administrator = User(login="other-object-author", name="other-object-author", role="ADMIN_OPERATOR", active=True,
                         password_hash="unused", permissions=[])
    db.add(administrator)
    db.flush()
    other = create_vehicle_type(db, "Другой ресурс", principal=administrator,
                                context=SaveContext("HUMAN", str(administrator.id), "api-test-other-resource"))
    db.commit()
    other_event = db.scalar(select(HistoryEvent.event_id).where(HistoryEvent.object_type == "TYPE", HistoryEvent.object_id == other.id))
    assert client.get("/api/arm2/history/DOC-KIND/" + other.id).status_code == 422
    assert client.get(PATH + "/" + other.id).status_code == 404
    assert client.get(f"/api/arm2/history/events/{other_event}?object_type=DOC-KIND&object_id={record['id']}").status_code == 403
    assert client.get("/api/arm2/history/TYPE/" + other.id).status_code == 403
    assert all(row["object_type"] == "DOC-KIND" for row in client.get(history).json())
    assert client.get("/api/arm2/history/events/99999999").status_code == 404
    assert counts(db) == (1, 6)
