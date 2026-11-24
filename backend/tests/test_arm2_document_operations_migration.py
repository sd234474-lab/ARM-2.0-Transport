from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa
from test_arm2_document_operations import correct, kind, operation, parent

from alembic import command
from app.config import settings


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


def test_0066_adds_historical_context_without_rewriting_0065_artifacts_and_roundtrips(isolated_arm2_database_url, monkeypatch):
    # Historical migrations are tested at their exact revisions, not by asking the
    # current CP API to run on a schema that predates mandatory explicit CP type.
    monkeypatch.setenv('DATABASE_URL', isolated_arm2_database_url)
    settings.cache_clear()
    assert ScriptDirectory.from_config(config()).get_heads() == ["0075"]
    command.downgrade(config(), '0065')
    engine = create_engine(isolated_arm2_database_url)
    with engine.begin() as db:
        for sql in [
            "INSERT INTO users(id,login,name,password_hash,role,active,permissions) VALUES(999,'legacy-fixture','Synthetic','unused','ADMIN_OPERATOR',true,'[]'::jsonb)",
            "INSERT INTO arm2_counterparties(id,name,status,version) VALUES('CP-700001','Historical CP','REVIEW',3)",
            "INSERT INTO arm2_document_kinds(id,name,applicable_object_types) VALUES('DOC-KIND-700001','Historical kind','[\"CP\"]'::jsonb)",
            "INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name,version) VALUES('DOCUMENT-700001','CP','CP-700001','DOC-KIND-700001','Historical document',2)",
            "INSERT INTO arm2_document_files(id,document_id,original_upload_name,stored_name,storage_ref,size,sha256,media_type,extension) VALUES('FILE-700001','DOCUMENT-700001','original.pdf','stored.pdf','disk:/SYNTHETIC/file',10,repeat('a',64),'application/pdf','pdf')",
            "INSERT INTO arm2_document_revisions(id,document_id,number,file_ids,author_id,source) VALUES('DOCUMENT-REV-700001','DOCUMENT-700001',1,'[\"FILE-700001\"]'::jsonb,999,'arm2-api-manual')",
            "INSERT INTO arm2_document_revision_files(revision_id,position,document_id,file_id) VALUES('DOCUMENT-REV-700001',0,'DOCUMENT-700001','FILE-700001')",
            "UPDATE arm2_documents SET current_revision_id='DOCUMENT-REV-700001' WHERE id='DOCUMENT-700001'",
            "INSERT INTO arm2_history_events(object_type,object_id,operation,actor_kind,actor_id,source,version_before,version_after,changes) VALUES('DOCUMENT','DOCUMENT-700001','UPDATED','HUMAN','999','synthetic-0065',1,2,'{\"current_revision_id\":{\"before\":null,\"after\":\"DOCUMENT-REV-700001\"}}'::jsonb)",
        ]:
            db.exec_driver_sql(sql)
    with engine.connect() as db:
        before = snapshot(db)
    command.upgrade(config(), "0066")
    with engine.connect() as db:
        assert snapshot(db) == before
        assert db.execute(text('SELECT parent_type,parent_id FROM arm2_document_revision_contexts')).one() == ('CP', 'CP-700001')
    command.downgrade(config(), '0065')
    command.upgrade(config(), '0066')
    with engine.connect() as db:
        assert snapshot(db) == before
    engine.dispose()


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
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0075"
    assert snapshot(db) == before
    assert db.scalar(text("SELECT count(*) FROM arm2_document_file_corrections")) == (
        1 if action == "correct" else 0
    )


def test_pending_b3c_acceptance_retries_after_refused_downgrade_without_changed_fingerprint_or_upload_contract(qa):
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
    with pytest.raises(RuntimeError, match='0070.*downgrade refused'):
        command.downgrade(config(), "0065")
    disk.fail_at = None
    assert accept(c, url, 1, [candidate], key).status_code == 200
    db.expire_all()
    assert db.get(FileAcceptance, key).fingerprint == fingerprint
    assert len(disk.uploads) == 1
    assert len(bundle(c, url)["revisions"]) == 1
