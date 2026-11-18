from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa
from test_arm2_document_operations import correct, kind, operation, parent

from alembic import command
from app.arm2.document_file_models import DocumentRevisionContext


def config():
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def snapshot(db):
    return {
        table: sorted(db.execute(text("SELECT row_to_json(t)::text FROM " + table + " t")).scalars())
        for table in (
            "arm2_documents",
            "arm2_document_files",
            "arm2_document_revisions",
            "arm2_document_revision_files",
            "arm2_history_events",
            "arm2_document_file_acceptances",
        )
    }


def test_0066_adds_historical_context_without_rewriting_0065_artifacts_and_roundtrips(qa):
    c, db, *_ = qa
    assert ScriptDirectory.from_config(config()).get_heads() == ["0068"]
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("legacy0065"))]).status_code == 200
    record = bundle(c, url)
    before = snapshot(db)
    db.commit()
    command.downgrade(config(), "0065")
    assert snapshot(db) == before
    db.commit()
    command.upgrade(config(), "0066")
    assert snapshot(db) == before
    context = db.scalar(select(DocumentRevisionContext))
    assert context.parent_type == "CP" and context.parent_id == qa[3]["cp"]
    assert bundle(c, url) == record


@pytest.mark.parametrize("action", ["archive_then_restore", "retype", "move", "correct"])
def test_0066_downgrade_never_discards_operations_or_correction_audit(qa, action):
    c, db, *_ = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf())]).status_code == 200
    if action == "archive_then_restore":
        assert operation(c, url, "archive", 2).status_code == 200
        assert operation(c, url, "restore", 3).status_code == 200
    elif action == "retype":
        assert operation(c, url, "retype", 2, kind_ref=kind(c)).status_code == 200
    elif action == "move":
        assert operation(c, url, "move", 2, target_type="CP", target_id=parent(c, "cp")).status_code == 200
    else:
        assert correct(c, url, 2, bundle(c, url)["revisions"][0]["files"][0]["id"]).status_code == 200
    before = snapshot(db)
    db.commit()
    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config(), "0065")
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0068"
    assert snapshot(db) == before
    assert db.scalar(text("SELECT count(*) FROM arm2_document_file_corrections")) == (
        1 if action == "correct" else 0
    )


def test_pending_b3c_acceptance_retries_after_0066_without_changed_fingerprint_or_upload_contract(qa):
    c, db, *_, disk = qa
    url = create(qa)
    candidate = stage(c, url, pdf("pending-before-0066"))
    import uuid

    from app.arm2.document_file_models import FileAcceptance

    key = str(uuid.uuid4())
    disk.fail_at = 0
    assert accept(c, url, 1, [candidate], key).status_code == 503
    attempt = db.get(FileAcceptance, key)
    # Preserve the exact private B3-C ledger shape: no CORRECT field existed in 0065.
    plan = dict(attempt.plan)
    plan.pop("correction", None)
    attempt.plan = plan
    fingerprint = attempt.fingerprint
    db.commit()
    command.downgrade(config(), "0065")
    command.upgrade(config(), "0066")
    disk.fail_at = None
    assert accept(c, url, 1, [candidate], key).status_code == 200
    db.expire_all()
    assert db.get(FileAcceptance, key).fingerprint == fingerprint
    assert len(disk.uploads) == 1
    assert len(bundle(c, url)["revisions"]) == 1
