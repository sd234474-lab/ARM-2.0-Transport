"""AUTHOR-RUN joint B2/B3/TYPE/B4 scenarios, using real authenticated HTTP and files."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_arm2_bulk import apply_body, reopen, upload
from test_arm2_document_file_api import accept, bundle, create, historical_revisions, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa
from test_arm2_document_operations import correct, kind, operation
from test_arm2_vehicle_links import command, current

from app.arm2.bulk_models import BulkReceipt
from app.arm2.history_models import HistoryEvent
from app.arm2.vehicle_type_models import VehicleType
from app.auth import hasher
from app.models import User


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def history(c, object_type, identifier):
    return checked(c.get(f"/api/arm2/history/{object_type}/{identifier}"))


def confirmed_type(c):
    return checked(c.post("/api/arm2/types", json={"name": "Integration car", "requires_sts": True,
        "requires_calibration": False, "requires_wialon": False, "plate_category": "Автомобильный"}), 201)


def save_row(c, workspace, **extra):
    body = apply_body(workspace, **extra)
    result = checked(c.post(f"/api/arm2/bulk/sessions/{workspace['id']}/apply", json=body))
    return result, body


def attach_type(c, vehicle, typ):
    w = upload(c, f"id\tГосномер\tTYPE — ID\n{vehicle['id']}\t{vehicle['plate_number']}\t{typ['id']}")
    save_row(c, w)
    return current(c, vehicle["id"])


def test_i01_type_owner_reopen_preserves_documents_and_exact_history(qa):
    c, db, _, parents, *_ = qa
    url = create(qa, "veh")
    before = bundle(c, url)
    typ = confirmed_type(c)
    v = attach_type(c, current(c, parents["veh"]), typ)
    initial_version = v["version"]
    for target in [("CP", parents["cp"]), ("ORG", parents["org"]), None]:
        old = v
        checked(command(c, v, target, confirm=bool(v["owner_cp_id"] or v["owner_org_id"])))
        v = current(c, v["id"])
        assert v["version"] == old["version"] + 1
        assert v["type_id"] == typ["id"] and bundle(c, url) == before
    assert v["version"] == initial_version + 3 and not v["owner_cp_id"] and not v["owner_org_id"]
    assert checked(c.get('/api/arm2/types/' + typ['id'])) == typ
    assert len(history(c, "VEH", v["id"])) == v["version"]
    db.expire_all()
    assert db.get(VehicleType, typ["id"]).plate_category == "Автомобильный"


def test_i02_bulk_vehicle_update_leaves_multifile_revisions_and_bytes_unchanged(qa):
    c, db, _, parents, *_ = qa
    url = create(qa, "veh")
    first, second = pdf("joint original"), pdf("joint second")
    checked(accept(c, url, 1, [stage(c, url, first), stage(c, url, second)]))
    original = bundle(c, url)
    checked(accept(c, url, 2, [{"file_id": original["revisions"][0]["files"][0]["id"]}]))
    before = bundle(c, url)
    events = history(c, "DOCUMENT", url.split('/')[-1])
    v = current(c, parents["veh"])
    w = upload(c, f"id\tГосномер\tМодель\n{v['id']}\t{v['plate_number']}\tIntegration model")
    result, body = save_row(c, w)
    assert result["version_after"] == v["version"] + 1
    assert checked(c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body)) == result
    assert bundle(c, url) == before and history(c, "DOCUMENT", url.split('/')[-1]) == events
    for row, content in zip(original["revisions"][0]["files"], [first, second], strict=True):
        r = c.get(url + f"/revisions/{original['revisions'][0]['id']}/files/{row['id']}/download")
        assert r.status_code == 200 and r.content == content
    fresh = current(c, v["id"])
    assert fresh["model"] == "Integration model" and fresh["version"] == v["version"] + 1
    db.expire_all()
    receipt = db.get(BulkReceipt, body["command_id"])
    event = db.get(HistoryEvent, receipt.event_id)
    assert event.correlation_id == body["command_id"] and set(event.changes) == {"model", "version"}
    assert reopen(c, w)["results"][0]["status"] == "SAVED"


def test_i03_b2_owner_change_invalidates_b4_stale_approval(qa):
    c, _, _, parents, *_ = qa
    v = current(c, parents["veh"])
    checked(command(c, v, ("CP", parents["cp"])))
    v = current(c, v["id"])
    w = upload(c, f"id\tplate_number\towner_cp_id\tmodel\n{v['id']}\t{v['plate_number']}\t{parents['cp']}\tStale proposal")
    stale = apply_body(w)
    checked(command(c, v, ("ORG", parents["org"]), confirm=True))
    r = c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=stale)
    assert r.status_code == 409, r.text
    fresh = reopen(c, w)
    assert fresh["approval_reset"] and fresh["confirmations"] == {} and fresh["decisions"] == {}
    assert current(c, v["id"])["owner_org_id"] == parents["org"]
    body = apply_body(fresh, fields={"owner": 0})
    rejected = c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body)
    assert rejected.status_code == 422 and "подтвердите" in rejected.json()["detail"]
    assert current(c, v["id"])["owner_org_id"] == parents["org"]


def test_i04_parallel_type_bulk_assignment_and_b2_owner_are_serialized(qa):
    c, db, _, parents, *_ = qa
    typ = confirmed_type(c)
    v = current(c, parents["veh"])
    w = upload(c, f"id\tГосномер\tTYPE — ID\n{v['id']}\t{v['plate_number']}\t{typ['id']}")
    body = apply_body(w)
    type_body = {k: typ[k] for k in ("name", "requires_sts", "requires_calibration", "requires_wialon")}
    type_body.update(expected_version=typ["version"], plate_category="Самоходная техника")
    with ThreadPoolExecutor(3) as pool:
        jobs = [pool.submit(c.put, '/api/arm2/types/' + typ['id'], json=type_body),
                pool.submit(c.post, f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body),
                pool.submit(command, c, v, ("CP", parents["cp"]))]
        responses = [job.result(20) for job in jobs]
    assert all(r.status_code in (200, 409, 422) for r in responses), [r.text for r in responses]
    fresh = current(c, v["id"])
    final_type = checked(c.get('/api/arm2/types/' + typ['id']))
    if fresh["type_id"]:
        assert fresh["type_id"] == typ["id"] and final_type["plate_category"] == "Автомобильный"
    assert len(history(c, "VEH", fresh["id"])) == fresh["version"]
    assert fresh["owner_cp_id"] in (None, parents["cp"])
    db.expire_all()


def test_i05_b3_move_retype_correct_and_b2_owner_isolate_histories_and_downloads(qa):
    c, _, _, parents, *_ = qa
    url = create(qa, "veh")
    checked(accept(c, url, 1, [stage(c, url, pdf("will correct")), stage(c, url, pdf("keep"))]))
    before = bundle(c, url)
    document_name = checked(c.get(url))["name"]
    vehicle_events = history(c, "VEH", parents["veh"])
    checked(operation(c, url, "retype", 2, kind_ref=kind(c)))
    target = checked(c.post('/api/arm2/vehicles', json={"name": "Second VEH", "plate_number": "A124BC86"}), 201)
    checked(operation(c, url, "move", 3, target_type="VEH", target_id=target["id"]))
    assert history(c, "VEH", parents["veh"]) == vehicle_events
    moved = f"/api/arm2/veh/{target['id']}/documents/{url.split('/')[-1]}"
    assert c.get(url).status_code == 404
    checked(command(c, target, ("ORG", parents["org"])))
    after = bundle(c, moved)['revisions']
    assert historical_revisions(after) == historical_revisions(before['revisions'])
    assert all(f["download_name"] == f"Second VEH {document_name}-{f['id']}.pdf" for f in after[0]["files"])
    files = before["revisions"][0]["files"]
    checked(correct(c, moved, 4, files[0]["id"]))
    rev = before["revisions"][0]["id"]
    assert c.get(moved + f"/revisions/{rev}/files/{files[0]['id']}/download").status_code == 404
    assert c.get(moved + f"/revisions/{rev}/files/{files[1]['id']}/download").content == pdf("keep")
    assert current(c, target["id"])["version"] == 2
    assert len(history(c, "DOCUMENT", moved.split('/')[-1])) == 5


def test_i07_two_authenticated_sessions_permissions_versions_and_no_duplicates(qa):
    c, db, user, parents, *_ = qa
    typ = confirmed_type(c)
    v = attach_type(c, current(c, parents["veh"]), typ)
    w = upload(c, f"id\tГосномер\tМодель\n{v['id']}\t{v['plate_number']}\tTwo sessions")
    with TestClient(c.app) as other:
        checked(other.post('/api/auth/login', json={"login": user.login, "password": "document-test-password"}))
        other.headers['X-CSRF-Token'] = other.cookies['csrf']
        type_change = {k: typ[k] for k in ("name", "plate_category", "requires_sts", "requires_calibration", "requires_wialon")}
        type_change.update(name="Renamed by the second session", expected_version=typ["version"])
        renamed = checked(other.put('/api/arm2/types/' + typ['id'], json=type_change))
        assert renamed["version"] == typ["version"] + 1
        assert c.put('/api/arm2/types/' + typ['id'], json=type_change).status_code == 409
        assert checked(c.get('/api/arm2/types/' + typ['id'])) == renamed
        assert len(history(c, 'TYPE', typ['id'])) == 2
        checked(command(other, v, ("CP", parents["cp"])))
        assert command(c, v, ("ORG", parents["org"])).status_code == 409
        assert c.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=apply_body(w)).status_code == 409
        refreshed = reopen(c, w)
        result, body = save_row(c, refreshed)
        assert checked(other.post(f"/api/arm2/bulk/sessions/{w['id']}/apply", json=body)) == result
        fresh = current(other, v['id'])
        assert checked(command(other, fresh, ("CP", parents['cp'])))["no_op"]
        assert current(c, v['id']) == fresh and len(history(c, 'VEH', v['id'])) == fresh['version']
    # A different authenticated account cannot reopen the private workspace.
    second = User(login='integration-other', name='Other', role='ADMIN_OPERATOR', active=True,
                  password_hash=hasher.hash('integration-other-password'), permissions=[])
    db.add(second)
    db.commit()
    with TestClient(c.app) as other:
        checked(other.post('/api/auth/login', json={'login': second.login, 'password': 'integration-other-password'}))
        other.headers['X-CSRF-Token'] = other.cookies['csrf']
        assert other.get(f"/api/arm2/bulk/sessions/{w['id']}").status_code == 403
    # Revoke the first account's permissions using an independent committed DB session.
    with Session(db.get_bind()) as admin:
        actor = admin.get(User, user.id)
        actor.role, actor.permissions = 'OPERATOR', []
        admin.commit()
    assert c.get(f"/api/arm2/bulk/sessions/{w['id']}").status_code == 403
    assert command(c, fresh, ("ORG", parents['org']), confirm=True).status_code == 403
