from sqlalchemy import func, select

from app.arm2.document_kind_models import DocumentKind
from app.arm2.history_models import HistoryEvent
from app.models import User

PATH = "/api/arm2/document-kinds"


def test_document_kind_search_all_rows_cyrillic_literals_order_and_history(admin, db):
    records = []
    for index in range(105):
        response = admin.post(PATH, json={"name": "Тарировка за сотой Ёж" if index == 104 else f"Вид испытания {105-index:03}",
                                          "applicable_object_types": ["VEH"], "max_current_documents": 1})
        assert response.status_code == 201
        records.append(response.json())
    page = admin.get(PATH + "/search").json()
    assert (page["total"], page["matched"], len(page["items"])) == (105, 105, 100)
    assert [row["id"] for row in page["items"]] == [row["id"] for row in records[:100]]
    assert admin.get(PATH + "/search?offset=100").json()["items"] == records[100:]
    for query in ["тарировка за сотой ёж", "ТАРИРОВКА ЗА СОТОЙ ЁЖ"]:
        found = admin.get(PATH + "/search", params={"q": query}).json()
        assert (found["total"], found["matched"], found["items"]) == (105, 1, [records[-1]])
    for name, query in [("Справка 100%", "%"), ("Прочий_вид", "_"), ("Путь\\оригинал", "\\"), ("Акт 'подрядчика'", "'")]:
        assert admin.post(PATH, json={"name": name, "applicable_object_types": ["CP", "ORG"], "max_current_documents": None}).status_code == 201
        result = admin.get(PATH + "/search", params={"q": query}).json()
        assert result["total"] == 109 and result["matched"] == 1 and result["items"][0]["name"] == name
    assert admin.get(PATH + "/search", params={"q": "нет такого имени"}).json()["matched"] == 0
    assert admin.get(PATH + "/search", params={"q": "  "}).json()["matched"] == 109
    assert admin.get(PATH + "/search?offset=200").json()["items"] == []
    for query in ["q=%00", "limit=0", "limit=101", "offset=-1", "offset=bad", "limit=1.5", "q=" + "x"*201]:
        assert admin.get(PATH + "/search?" + query).status_code == 422
    assert db.scalar(select(func.count()).select_from(DocumentKind)) == 109
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 109  # GET/search never appends events.


def test_document_kind_search_permission_and_static_route(client, users, db):
    assert client.get(PATH + "/search").status_code == 401
    operator = db.scalar(select(User).where(User.login == "operator"))
    assert client.post("/api/auth/login", json={"login": "operator", "password": "test-password-123"}).status_code == 200
    for permissions in [[], ["arm2_doc_kind_edit"], ["arm2_type_view"], ["arm2_doc_kind_view"]]:
        operator.permissions = permissions
        db.commit()
        response = client.get(PATH + "/search")
        if permissions == ["arm2_doc_kind_view"]:
            assert response.status_code == 200
            assert response.json() == {"items": [], "total": 0, "matched": 0, "limit": 100, "offset": 0}
        else:
            assert response.status_code == 403
