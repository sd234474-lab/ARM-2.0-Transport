from sqlalchemy import event, func, select

from app.arm2.history_models import HistoryEvent
from app.arm2.representative_models import CounterpartyRepresentative
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


def all_permissions():
    return [
        Permission.ARM2_TYPE_VIEW,
        Permission.ARM2_TYPE_EDIT,
        Permission.ARM2_VEH_VIEW,
        Permission.ARM2_VEH_EDIT,
        Permission.ARM2_CP_VIEW,
        Permission.ARM2_CP_EDIT,
        Permission.ARM2_ORG_VIEW,
        Permission.ARM2_ORG_EDIT,
    ]


def seed_all_object_types(client, db):
    set_operator(db, all_permissions())
    sign_in(client)
    vehicle_type = client.post(
        "/api/arm2/types",
        json={
            "name": "History type",
            "requires_sts": True,
            "requires_calibration": True,
            "requires_wialon": True,
        },
    ).json()
    vehicle = client.post(
        "/api/arm2/vehicles",
        json={
            "name": "History vehicle",
            "plate_number": "B123BC86",
            "type_id": vehicle_type["id"],
            "accounting_state": "Действующая",
            "model": None,
            "wialon_unit_id": None,
        },
    ).json()
    counterparty = client.post(
        "/api/arm2/counterparties", json={"name": "History CP"}
    ).json()
    organization = client.post(
        "/api/arm2/organizations", json={"name": "History ORG"}
    ).json()
    cp_representative = client.post(
        f"/api/arm2/counterparties/{counterparty['id']}/representatives",
        json={"name": "CP representative"},
    ).json()
    org_representative = client.post(
        f"/api/arm2/organizations/{organization['id']}/representatives",
        json={"name": "ORG representative"},
    ).json()
    return {
        "TYPE": vehicle_type["id"],
        "VEH": vehicle["id"],
        "CP": counterparty["id"],
        "ORG": organization["id"],
        "CPREP": cp_representative["id"],
        "ORGREP": org_representative["id"],
    }


def event_id(db, object_type, object_id):
    return db.scalar(
        select(HistoryEvent.event_id).where(
            HistoryEvent.object_type == object_type,
            HistoryEvent.object_id == object_id,
        )
    )


def test_history_auth_independent_permissions_and_no_global_endpoint(
    client, users, db
):
    assert client.get("/api/arm2/history/TYPE/TYPE-000001").status_code == 401
    assert client.get("/api/arm2/history/events/1").status_code == 401
    objects = seed_all_object_types(client, db)
    events = {
        object_type: event_id(db, object_type, object_id)
        for object_type, object_id in objects.items()
    }

    permission_cases = (
        (Permission.ARM2_TYPE_VIEW, {"TYPE"}),
        (Permission.ARM2_VEH_VIEW, {"VEH"}),
        (Permission.ARM2_CP_VIEW, {"CP", "CPREP"}),
        (Permission.ARM2_ORG_VIEW, {"ORG", "ORGREP"}),
    )
    for permission, allowed_types in permission_cases:
        set_operator(db, [permission])
        for object_type, object_id in objects.items():
            response = client.get(
                f"/api/arm2/history/{object_type}/{object_id}"
            )
            expected = 200 if object_type in allowed_types else 403
            assert response.status_code == expected
        for object_type, history_event_id in events.items():
            response = client.get(
                f"/api/arm2/history/events/{history_event_id}"
            )
            expected = 200 if object_type in allowed_types else 403
            assert response.status_code == expected

    statements = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    set_operator(db, [Permission.ARM2_TYPE_VIEW])
    event.listen(engine, "before_cursor_execute", capture)
    try:
        denied = client.get(f"/api/arm2/history/events/{events['CP']}")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert denied.status_code == 403
    history_queries = [
        statement
        for statement in statements
        if "arm2_history_events" in statement
    ]
    assert len(history_queries) == 1
    assert "changes" not in history_queries[0]
    assert "related_objects" not in history_queries[0]
    assert "correlation_id" not in history_queries[0]

    set_operator(
        db,
        [
            Permission.ARM2_TYPE_VIEW,
            Permission.ARM2_VEH_VIEW,
            Permission.ARM2_CP_VIEW,
            Permission.ARM2_ORG_VIEW,
        ],
    )
    assert client.get("/api/arm2/history").status_code == 404
    assert client.get(
        "/api/arm2/history/UNKNOWN/TYPE-000001"
    ).status_code == 422


def test_history_paging_order_whitelists_detail_and_read_stability(
    client, users, db
):
    set_operator(db, [Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT])
    sign_in(client)
    created = client.post(
        "/api/arm2/counterparties", json={"name": "History CP"}
    ).json()
    confirmed = client.put(
        f"/api/arm2/counterparties/{created['id']}",
        json={
            "expected_version": 1,
            "name": created["name"],
            "full_name": "History CP Full",
            "legal_address": "History address",
        },
    ).json()
    no_op = client.put(
        f"/api/arm2/counterparties/{created['id']}",
        json={"expected_version": 2, "name": created["name"]},
    )
    assert no_op.status_code == 200 and no_op.json()["version"] == 2
    reviewed = client.put(
        f"/api/arm2/counterparties/{created['id']}",
        json={
            "expected_version": 2,
            "name": created["name"],
            "legal_address": None,
        },
    ).json()
    assert reviewed["version"] == 3 and reviewed["status"] == "REVIEW"

    before_count = db.scalar(select(func.count()).select_from(HistoryEvent))
    page = client.get(
        f"/api/arm2/history/CP/{created['id']}?limit=2&offset=0"
    )
    assert page.status_code == 200, page.text
    items = page.json()
    assert len(items) == 2
    assert items[0]["event_id"] > items[1]["event_id"]
    summary_fields = {
        "event_id",
        "object_type",
        "object_id",
        "operation",
        "occurred_at",
        "actor_kind",
        "actor_id",
        "source",
        "version_before",
        "version_after",
    }
    assert all(set(item) == summary_fields for item in items)
    assert "changes" not in items[0]
    assert "related_objects" not in items[0]
    assert "correlation_id" not in items[0]

    next_page = client.get(
        f"/api/arm2/history/CP/{created['id']}?limit=2&offset=2"
    )
    assert next_page.status_code == 200
    assert len(next_page.json()) == 1
    all_event_ids = [item["event_id"] for item in items + next_page.json()]
    assert all_event_ids == sorted(all_event_ids, reverse=True)

    detail = client.get(f"/api/arm2/history/events/{items[0]['event_id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert set(detail_body) == summary_fields | {
        "correlation_id",
        "changes",
        "related_objects",
    }
    assert detail_body["object_type"] == "CP"
    assert detail_body["object_id"] == created["id"]
    assert isinstance(detail_body["changes"], dict)
    assert isinstance(detail_body["related_objects"], list)

    assert client.get("/api/arm2/history/events/999999999").status_code == 404
    assert client.get("/api/arm2/history/CP/CP-999999").json() == []
    assert client.get("/api/arm2/history/CP/ORG-000001").status_code == 422
    assert client.get(
        f"/api/arm2/history/CP/{created['id']}?limit=0"
    ).status_code == 422
    assert client.get(
        f"/api/arm2/history/CP/{created['id']}?offset=-1"
    ).status_code == 422

    db.expire_all()
    assert client.get(f"/api/arm2/counterparties/{created['id']}").json()[
        "version"
    ] == 3
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before_count
    assert confirmed["version"] == 2


def test_card_get_does_not_query_history_and_rep_history_uses_parent_rights(
    client, users, db
):
    objects = seed_all_object_types(client, db)
    engine = db.get_bind()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(f"/api/arm2/counterparties/{objects['CP']}")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert not any("arm2_history_events" in statement for statement in statements)

    cp_rep_event = event_id(db, "CPREP", objects["CPREP"])
    org_rep_event = event_id(db, "ORGREP", objects["ORGREP"])
    set_operator(db, [Permission.ARM2_CP_VIEW])
    assert client.get(
        f"/api/arm2/history/CPREP/{objects['CPREP']}"
    ).status_code == 200
    assert client.get(
        f"/api/arm2/history/events/{cp_rep_event}"
    ).status_code == 200
    assert client.get(
        f"/api/arm2/history/ORGREP/{objects['ORGREP']}"
    ).status_code == 403
    assert client.get(
        f"/api/arm2/history/events/{org_rep_event}"
    ).status_code == 403

    set_operator(db, [Permission.ARM2_ORG_VIEW])
    assert client.get(
        f"/api/arm2/history/ORGREP/{objects['ORGREP']}"
    ).status_code == 200
    assert client.get(
        f"/api/arm2/history/CPREP/{objects['CPREP']}"
    ).status_code == 403

    set_operator(db, [Permission.ARM2_CP_VIEW])
    representative = db.get(CounterpartyRepresentative, objects["CPREP"])
    db.delete(representative)
    db.commit()
    assert client.get(
        f"/api/arm2/history/CPREP/{objects['CPREP']}"
    ).status_code == 404
    assert client.get(
        f"/api/arm2/history/events/{cp_rep_event}"
    ).status_code == 404
