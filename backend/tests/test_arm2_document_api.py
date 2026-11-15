import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.history_models import HistoryEvent
from app.auth import hasher
from app.config import settings
from app.db import engine
from app.models import User


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
            user = User(
                login="document-admin",
                name="document-admin",
                password_hash=hasher.hash("document-test-password"),
                role="ADMIN_OPERATOR",
                active=True,
                permissions=[],
            )
            db.add(user)
            db.commit()
            with TestClient(app) as client:
                assert (
                    client.post(
                        "/api/auth/login", json={"login": user.login, "password": "document-test-password"}
                    ).status_code
                    == 200
                )
                client.headers["X-CSRF-Token"] = client.cookies["csrf"]
                parents = {}
                for token, route, body in [
                    ("cp", "counterparties", {"name": "CP"}),
                    ("org", "organizations", {"name": "ORG"}),
                    ("veh", "vehicles", {"name": "VEH", "plate_number": "A123BC86"}),
                ]:
                    response = client.post("/api/arm2/" + route, json=body)
                    assert response.status_code == 201, response.text
                    parents[token] = response.json()["id"]
                response = client.post(
                    "/api/arm2/document-kinds",
                    json={
                        "name": "Общий тип",
                        "applicable_object_types": ["CP", "ORG", "VEH"],
                        "max_current_documents": None,
                    },
                )
                assert response.status_code == 201, response.text
                yield client, db, user, parents, response.json()["id"]
    finally:
        if engine.cache_info().currsize:
            engine().dispose()
        engine.cache_clear()
        settings.cache_clear()


def path(token, parent):
    return f"/api/arm2/{token}/{parent}/documents"


@pytest.mark.parametrize("token", ["cp", "org", "veh"])
def test_document_zero_files_metadata_history_noop_and_binding(api, token):
    client, db, user, parents, kind = api
    base = path(token, parents[token])
    body = {"kind_ref": kind, "name": "  Без файла  ", "description": "  Текст\nописания  "}
    r = client.post(base, json=body)
    assert r.status_code == 201, r.text
    record = r.json()
    assert record == {
        "id": "DOCUMENT-000001",
        "parent_type": token.upper(),
        "parent_id": parents[token],
        "kind_ref": kind,
        "kind_name": "Общий тип",
        "name": "Без файла",
        "description": body["description"],
        "archive_state": "Неархивный",
        "display_state": "Не загружен",
        "version": 1,
    }
    assert client.get(base).json() == [record]
    assert client.get(base + "?archive=archived").json() == []
    assert client.get(base + "?archive=all").json() == [record]
    assert client.get(base + "/DOCUMENT-000001").json() == record
    update = {"expected_version": 1, "name": "Новое имя", "description": None}
    r = client.put(base + "/" + record["id"], json=update)
    assert r.status_code == 200 and r.json()["version"] == 2
    assert (
        client.put(base + "/" + record["id"], json={**update, "expected_version": 2}).json()["version"] == 2
    )
    assert client.put(base + "/" + record["id"], json=update).status_code == 409
    history = client.get("/api/arm2/history/DOCUMENT/" + record["id"])
    assert history.status_code == 200 and len(history.json()) == 2
    detail = client.get("/api/arm2/history/events/" + str(history.json()[0]["event_id"])).json()
    assert detail["changes"] == {
        "name": {"before": "Без файла", "after": "Новое имя"},
        "description": {"before": body["description"], "after": None},
        "version": {"before": 1, "after": 2},
    }
    assert (detail["actor_id"], detail["source"]) == (str(user.id), "arm2-api")
    other = "org" if token != "org" else "cp"
    assert client.get(path(other, parents[other]) + "/" + record["id"]).status_code == 404
    assert client.put(path(other, parents[other]) + "/" + record["id"], json=update).status_code == 404
    assert db.scalar(select(func.count()).select_from(Document)) == 1


def test_document_validation_applicability_limits_and_retrospective_api(api):
    c, db, _, p, kind = api
    base = path("cp", p["cp"])
    current = c.get("/api/arm2/document-kinds/" + kind).json()
    rules = {k: v for k, v in current.items() if k not in ["id", "version"]}
    assert (
        c.put(
            "/api/arm2/document-kinds/" + kind,
            json={**rules, "expected_version": 1, "max_current_documents": 1},
        ).status_code
        == 200
    )
    body = {"kind_ref": kind, "name": "Первый", "description": None}
    assert c.post(base, json=body).status_code == 201
    assert c.post(base, json={**body, "name": "Второй"}).status_code == 422
    before = db.scalar(select(func.count()).select_from(HistoryEvent))
    rejected = c.put(
        "/api/arm2/document-kinds/" + kind,
        json={**rules, "expected_version": 2, "applicable_object_types": ["ORG"]},
    )
    assert rejected.status_code == 422 and "Правила не сохранены" in rejected.json()["detail"]
    assert c.get("/api/arm2/document-kinds/" + kind).json()["version"] == 2
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before
    for bad in [
        {**body, "name": " "},
        {**body, "kind_ref": "DOC-KIND-999999"},
        {**body, "parent_id": p["org"]},
        {**body, "archive_state": "Архивный"},
        {**body, "actor_id": "999"},
        {**body, "file": "fake"},
    ]:
        assert c.post(base, json=bad).status_code == 422
    assert c.post(path("cp", "CP-999999"), json=body).status_code == 404
    assert c.post(path("cp", p["org"]), json=body).status_code == 422
    assert c.get(base + "?limit=101").status_code == 422
    assert c.get(base + "?offset=-1").status_code == 422
    assert db.scalar(select(func.count()).select_from(Document)) == 1
    renamed = c.put(
        "/api/arm2/document-kinds/" + kind,
        json={**rules, "expected_version": 2, "name": "Только переименование", "max_current_documents": 1},
    )
    assert renamed.status_code == 200 and renamed.json()["version"] == 3
    assert (
        c.put(
            "/api/arm2/document-kinds/" + kind,
            json={
                **rules,
                "expected_version": 3,
                "name": "Только переименование",
                "max_current_documents": 1,
            },
        ).json()["version"]
        == 3
    )
    assert c.get(base).json()[0]["kind_name"] == "Только переименование"
    assert c.get(base).json()[0]["version"] == 1
    for field, value in [("kind_ref", kind), ("parent_id", p["org"]), ("archive_state", "Архивный")]:
        assert (
            c.put(
                base + "/DOCUMENT-000001",
                json={"expected_version": 1, "name": "Первый", "description": None, field: value},
            ).status_code
            == 422
        )


def test_document_parent_typed_access_picker_and_history(api):
    c, db, admin, parents, kind = api
    base = path("cp", parents["cp"])
    record = c.post(base, json={"kind_ref": kind, "name": "Запись", "description": None}).json()
    event = c.get("/api/arm2/history/DOCUMENT/" + record["id"]).json()[0]["event_id"]
    operator = User(
        login="document-operator",
        name="document-operator",
        password_hash=hasher.hash("document-test-password"),
        role="OPERATOR",
        active=True,
        permissions=[],
    )
    db.add(operator)
    db.commit()
    assert (
        c.post(
            "/api/auth/login", json={"login": operator.login, "password": "document-test-password"}
        ).status_code
        == 200
    )
    c.headers["X-CSRF-Token"] = c.cookies["csrf"]
    reads = [
        base,
        base + "/" + record["id"],
        base + "/kinds",
        "/api/arm2/history/DOCUMENT/" + record["id"],
        "/api/arm2/history/events/" + str(event),
    ]
    for grants in [
        [],
        ["arm2_doc_kind_view", "arm2_doc_kind_edit"],
        ["arm2_document_view"],
        ["arm2_cp_view"],
        ["arm2_org_view", "arm2_document_view"],
        ["arm2_cp_view", "arm2_document_edit"],
    ]:
        operator.permissions = grants
        db.commit()
        for url in reads:
            assert c.get(url).status_code == 403, (grants, url)
        assert c.post(base, json={"kind_ref": kind, "name": "Denied", "description": None}).status_code == 403
    operator.permissions = ["arm2_cp_view", "arm2_document_view"]
    db.commit()
    for url in reads:
        assert c.get(url).status_code == 200, url
    assert c.get("/api/arm2/document-kinds").status_code == 403
    assert c.post(base, json={"kind_ref": kind, "name": "Denied", "description": None}).status_code == 403
    operator.permissions = ["arm2_cp_view", "arm2_cp_edit", "arm2_document_view", "arm2_document_edit"]
    db.commit()
    assert c.post(base, json={"kind_ref": kind, "name": "Allowed", "description": None}).status_code == 201
    assert db.scalar(select(func.count()).select_from(Document)) == 2


def test_scoped_picker_and_document_pagination_search_the_complete_parent_selection(api):
    client, db, _, parents, kind = api
    for number in range(2, 107):
        db.add(
            DocumentKind(
                id=f"DOC-KIND-{number:06}",
                name="Цель за сотой" if number == 106 else f"Кандидат {number}",
                applicable_object_types=["ORG"] if number == 3 else ["CP"],
                max_current_documents=None,
                version=1,
            )
        )
    for number in range(1, 106):
        db.add(
            Document(
                id=f"DOCUMENT-{number:06}",
                parent_type="CP",
                parent_id=parents["cp"],
                kind_ref=kind,
                name=f"Фикстура {number}",
                description=None,
                archive_state="Неархивный",
                version=1,
            )
        )
    db.commit()
    base = path("cp", parents["cp"])
    first = client.get(base + "/kinds?limit=100&offset=0").json()
    second = client.get(base + "/kinds?limit=100&offset=100").json()
    assert len(first) == 100 and len(second) == 5
    assert not any(row["id"] == "DOC-KIND-000003" for row in first + second)
    assert client.get(base + "/kinds?q=ЦЕЛЬ").json() == [second[-1]]
    assert len(client.get(base + "?limit=100&offset=0").json()) == 100
    assert [row["id"] for row in client.get(base + "?limit=100&offset=100").json()] == [
        f"DOCUMENT-{number:06}" for number in range(101, 106)
    ]
    assert client.get(path("org", parents["org"])).json() == []
