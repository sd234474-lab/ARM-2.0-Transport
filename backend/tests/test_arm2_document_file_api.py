import hashlib
import io
import shutil
import uuid
import zipfile

import pytest
from PIL import Image
from sqlalchemy import func, select
from test_arm2_document_api import api as api  # Reuse real login, parent records and disposable DB.

from app.arm2 import document_file_storage as storage
from app.arm2.document_file_models import DocumentFile, DocumentRevision, FileAcceptance
from app.arm2.document_models import Document
from app.arm2.history_models import HistoryEvent


def pdf(text="first"):
    return b"%PDF-1.4\n" + text.encode() + b"\n%%EOF\n"


def png():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(out, format="PNG")
    return out.getvalue()


def docx():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return out.getvalue()


class FakeDisk:
    """Deterministic local bytes, never a live cloud credential/client."""

    def __init__(self, path):
        self.path = path
        path.mkdir()
        self.uploads = []
        self.fail_at = None

    def location(self, ref):
        return self.path / hashlib.sha256(ref.encode()).hexdigest()

    def metadata(self, ref):
        path = self.location(ref)
        if not path.exists():
            return None
        data = path.read_bytes()
        return {"type": "file", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def upload(self, path, ref, digest):
        if self.fail_at is not None and len(self.uploads) >= self.fail_at:
            raise RuntimeError("injected cloud timeout")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        self.uploads.append(ref)
        shutil.copyfile(path, self.location(ref))

    def download_document(self, ref, target, digest, size):
        shutil.copyfile(self.location(ref), target)


@pytest.fixture
def qa(api, monkeypatch, tmp_path):
    fake = FakeDisk(tmp_path / "disk")
    monkeypatch.setattr(storage, "storage", lambda: fake)
    monkeypatch.setenv("ARM2_DOCUMENT_DISK_ROOT", "ARM2_TEST/B3_C")
    return (*api, fake)


def create(qa, token="cp"):
    c, db, user, parents, kind, fake = qa
    base = f"/api/arm2/{token}/{parents[token]}/documents"
    r = c.post(base, json={"kind_ref": kind, "name": "Без файла", "description": None})
    assert r.status_code == 201, r.text
    return base + "/" + r.json()["id"]


def stage(c, url, data, name="  Исходный файл.pdf  "):
    r = c.post(url + "/stages", json={"original_upload_name": name, "size": len(data)})
    assert r.status_code == 201, r.text
    stage_id = r.json()["id"]
    r = c.put(
        url + "/stages/" + stage_id + "/content",
        content=data,
        headers={"Content-Type": "application/x-spoof"},
    )
    assert r.status_code == 200, r.text
    preview = c.get(url + "/stages/" + stage_id + "/preview")
    assert preview.status_code == 200 and preview.content == data
    assert preview.headers["content-security-policy"].startswith("sandbox")
    assert preview.headers["x-content-type-options"] == "nosniff"
    return {"stage_id": stage_id}


def accept(c, url, version, selection, key=None):
    return c.post(
        url + "/accept",
        json={"expected_version": version, "idempotency_key": key or str(uuid.uuid4()), "files": selection},
    )


def bundle(c, url):
    r = c.get(url + "/files")
    assert r.status_code == 200, r.text
    return r.json()


def historical_revisions(revisions):
    """All historical metadata, excluding only D's new current-name projection."""
    return [{**r, 'files':[{k:v for k,v in f.items() if k != 'download_name'} for f in r['files']]}
            for r in revisions]


@pytest.mark.parametrize("token", ["cp", "org", "veh"])
def test_zero_file_accept_full_revisions_immutable_history_and_roundtrip(qa, token):
    c, db, user, parents, kind, fake = qa
    url = create(qa, token)
    assert bundle(c, url)["revisions"] == []
    contents = [pdf(), png(), docx()]
    candidates = [stage(c, url, data, f"original-{i}.bin") for i, data in enumerate(contents)]
    r = accept(c, url, 1, candidates)
    assert r.status_code == 200, r.text
    assert r.json()["document_version"] == 2
    first = bundle(c, url)
    assert first["display_state"] == "Доступен" and len(first["revisions"]) == 1
    rev1 = first["revisions"][0]
    assert rev1["number"] == 1 and rev1["author_id"] == user.id and rev1["source"] == "arm2-api-manual"
    assert len(rev1["files"]) == 3
    assert [row["extension"] for row in rev1["files"]] == ["pdf", "png", "docx"]
    refs = {row.id: row.storage_ref for row in db.scalars(select(DocumentFile))}
    for index, row in enumerate(rev1["files"]):
        r = c.get(url + f"/revisions/{rev1['id']}/files/{row['id']}/download")
        assert r.status_code == 200 and r.content == contents[index]
        assert row["sha256"] == hashlib.sha256(contents[index]).hexdigest()
        assert row["original_upload_name"] == f"original-{index}.bin"
    assert "storage_ref" not in str(first) and "disk:/" not in str(first)
    replacement = stage(c, url, pdf("replacement"), "замена.pdf")
    selected = [{"file_id": rev1["files"][0]["id"]}, replacement, {"file_id": rev1["files"][2]["id"]}]
    r = accept(c, url, 2, selected)
    assert r.status_code == 200, r.text
    second = bundle(c, url)
    assert second["revisions"][1] == rev1
    rev2 = second["revisions"][0]
    assert rev2["number"] == 2 and [rev2["files"][i]["id"] for i in [0, 2]] == [
        rev1["files"][i]["id"] for i in [0, 2]
    ]
    assert len(fake.uploads) == 4
    for row in db.scalars(select(DocumentFile)):
        if row.id in refs:
            assert row.storage_ref == refs[row.id]
    assert c.get(url + f"/revisions/{rev2['id']}/files/{rev1['files'][1]['id']}/download").status_code == 404
    assert (
        c.get(url + f"/revisions/{rev1['id']}/files/{rev1['files'][1]['id']}/download").content == contents[1]
    )
    added = stage(c, url, pdf("added"), "добавление.pdf")
    assert accept(c, url, 3, [*({"file_id": row["id"]} for row in rev2["files"]), added]).status_code == 200
    assert len(bundle(c, url)["revisions"][0]["files"]) == 4
    assert accept(c, url, 4, []).status_code == 200
    empty = bundle(c, url)
    assert (
        empty["display_state"] == "Не загружен"
        and empty["revisions"][0]["files"] == []
        and len(empty["revisions"]) == 4
    )
    assert db.scalar(select(func.count()).select_from(Document)) == 1
    events = c.get("/api/arm2/history/DOCUMENT/" + url.split("/")[-1]).json()
    assert len(events) == 5
    detail = c.get("/api/arm2/history/events/" + str(events[0]["event_id"])).json()
    assert set(detail["changes"]) == {"current_revision_id", "version"}
    assert detail["actor_id"] == str(user.id) and detail["source"] == "arm2-api-manual"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 4


def test_noop_sha_deduplication_idempotency_and_stale_version(qa):
    c, db, _, _, _, fake = qa
    url = create(qa)
    item = stage(c, url, pdf())
    key = str(uuid.uuid4())
    first = accept(c, url, 1, [item], key)
    assert first.status_code == 200
    assert accept(c, url, 1, [item], key).json() == first.json()
    assert accept(c, url, 1, [], key).status_code == 409
    original = bundle(c, url)["revisions"][0]["files"][0]
    same = stage(c, url, pdf(), "другое имя.pdf")
    r = accept(c, url, 2, [same])
    assert r.status_code == 200 and r.json()["no_op"] and r.json()["document_version"] == 2
    assert len(fake.uploads) == 1 and len(bundle(c, url)["revisions"]) == 1
    duplicate = stage(c, url, pdf())
    assert accept(c, url, 2, [{"file_id": original["id"]}, duplicate]).status_code == 422
    assert accept(c, url, 1, [{"file_id": original["id"]}]).status_code == 409
    assert (
        db.scalar(
            select(func.count()).select_from(HistoryEvent).where(HistoryEvent.object_type == "DOCUMENT")
        )
        == 2
    )


def test_partial_upload_retry_missing_corrupt_remote_and_immutable_names(qa):
    c, db, _, parents, kind, fake = qa
    url = create(qa)
    candidates = [stage(c, url, pdf("1")), stage(c, url, pdf("2"))]
    key = str(uuid.uuid4())
    fake.fail_at = 1
    assert accept(c, url, 1, candidates, key).status_code == 503
    assert bundle(c, url)["revisions"] == [] and c.get(url).json()["version"] == 1
    assert db.scalar(select(func.count()).select_from(DocumentRevision)) == 0
    fake.fail_at = None
    assert accept(c, url, 1, candidates, key).status_code == 200
    assert len(fake.uploads) == 2
    rev = bundle(c, url)["revisions"][0]
    row = db.get(DocumentFile, rev["files"][0]["id"])
    ref, name = row.storage_ref, row.stored_name
    setting = c.get("/api/arm2/document-kinds/" + kind).json()
    assert (
        c.put(
            "/api/arm2/document-kinds/" + kind,
            json={
                **{k: v for k, v in setting.items() if k not in {"id", "version"}},
                "expected_version": setting["version"],
                "file_naming_template": "PARENT_AND_KIND",
            },
        ).status_code
        == 200
    )
    assert (
        c.put(
            "/api/arm2/counterparties/" + parents["cp"],
            json={"expected_version": 1, "name": "Новое имя родителя"},
        ).status_code
        == 200
    )
    r = c.get(url + f"/revisions/{rev['id']}/files/{row.id}/download")
    assert r.status_code == 200 and "filename" in r.headers["content-disposition"]
    assert "Новое имя родителя" in __import__("urllib.parse", fromlist=["unquote"]).unquote(
        r.headers["content-disposition"]
    )
    db.refresh(row)
    assert (row.storage_ref, row.stored_name) == (ref, name)
    assert c.get(url).json()["version"] == 2
    fake.location(ref).write_bytes(b"corrupted")
    assert bundle(c, url)["display_state"] == "Файл недоступен"
    assert c.get(url + f"/revisions/{rev['id']}/files/{row.id}/download").status_code == 503
    fake.location(ref).unlink()
    assert bundle(c, url)["display_state"] == "Файл недоступен"
    assert db.scalar(select(func.count()).select_from(FileAcceptance)) == 1


@pytest.mark.parametrize("name", ["../escape.pdf", "C:\\secret.pdf", "/absolute.pdf", "bad\x00.pdf"])
def test_path_traversal_is_rejected_without_staging(qa, name):
    c, *_ = qa
    url = create(qa)
    assert c.post(url + "/stages", json={"original_upload_name": name, "size": 10}).status_code == 422


def test_mime_spoof_bounded_stream_empty_bundle_and_private_root(qa, monkeypatch):
    c, *_ = qa
    url = create(qa)
    r = c.post(url + "/stages", json={"original_upload_name": "spoof.pdf", "size": 14})
    sid = r.json()["id"]
    assert (
        c.put(
            url + "/stages/" + sid + "/content",
            content=b"<html></html>!",
            headers={"Content-Type": "application/pdf"},
        ).status_code
        == 422
    )
    assert c.get(url + "/stages/" + sid + "/preview").status_code == 422
    assert c.put(url + "/stages/" + sid + "/content", content=b"x" * 15).status_code == 413
    monkeypatch.setenv("MAX_FILE_BYTES", "32")
    storage.settings.cache_clear()
    assert c.post(url + "/stages", json={"original_upload_name": "big.pdf", "size": 33}).status_code == 413
    result = accept(c, url, 1, [])
    assert result.status_code == 200 and result.json()["no_op"]
    assert bundle(c, url)["revisions"] == []
    monkeypatch.delenv("ARM2_DOCUMENT_DISK_ROOT")
    with pytest.raises(storage.StorageUnavailable):
        storage.document_root()


def test_sql_commit_failure_keeps_previous_pointer_and_private_retry_ledger(qa, monkeypatch):
    from sqlalchemy.orm import Session

    c, db, _, _, _, fake = qa
    url = create(qa)
    candidate = stage(c, url, pdf("commit-failure"))
    original = Session.commit

    def failing_commit(session):
        if any(
            isinstance(row, FileAcceptance) and row.state == "accepted"
            for row in session.identity_map.values()
        ):
            session.rollback()
            raise RuntimeError("injected SQL commit failure")
        return original(session)

    key = str(uuid.uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="injected SQL commit failure"):
            accept(c, url, 1, [candidate], key)
    assert bundle(c, url)["revisions"] == [] and c.get(url).json()["version"] == 1
    db.expire_all()
    assert db.get(FileAcceptance, key).state == "uploaded"
    assert len(fake.uploads) == 1
    assert accept(c, url, 1, [candidate], key).status_code == 200
    assert len(fake.uploads) == 1


def test_uncertain_committed_response_replays_result_without_new_revision(qa, monkeypatch):
    from sqlalchemy.orm import Session

    c, db, _, _, _, fake = qa
    url = create(qa)
    candidate = stage(c, url, pdf("acknowledgement lost"))
    original = Session.commit

    def lost_response(session):
        published = any(
            isinstance(row, FileAcceptance) and row.state == "accepted"
            for row in session.identity_map.values()
        )
        original(session)
        if published:
            raise RuntimeError("injected lost acknowledgement after successful commit")

    key = str(uuid.uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", lost_response)
        with pytest.raises(RuntimeError, match="lost acknowledgement"):
            accept(c, url, 1, [candidate], key)
    assert c.get(url).json()["version"] == 2 and len(bundle(c, url)["revisions"]) == 1
    assert accept(c, url, 1, [candidate], key).status_code == 200
    assert len(fake.uploads) == 1
    assert len(c.get("/api/arm2/history/DOCUMENT/" + url.split("/")[-1]).json()) == 2
