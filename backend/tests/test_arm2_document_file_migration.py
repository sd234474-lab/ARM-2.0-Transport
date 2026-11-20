import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa

from alembic import command
from app.config import settings


def config():
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def test_accepted_rows_complete_manifest_and_typed_file_fks_are_immutable(qa):
    c, db, *_ = qa
    url = create(qa)
    assert accept(c, url, 1, [stage(c, url, pdf("original"))]).status_code == 200
    previous = bundle(c, url)["revisions"][0]
    assert accept(c, url, 2, [stage(c, url, pdf("replacement"))]).status_code == 200
    current = bundle(c, url)["revisions"][0]
    for table in ["arm2_document_files", "arm2_document_revisions", "arm2_document_revision_files"]:
        for operation in [
            f"DELETE FROM {table}",
            f"UPDATE {table} SET " + ("position=position" if table.endswith("revision_files") else "id=id"),
        ]:
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text(operation))
    # A late INSERT is also forbidden, even with a valid historical FILE from this document.
    with pytest.raises(DBAPIError, match="manifest"), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO arm2_document_revision_files(revision_id,document_id,file_id,position) VALUES(:revision,:document,:file,1)"
            ),
            {"revision": current["id"], "document": url.split("/")[-1], "file": previous["files"][0]["id"]},
        )
        db.execute(text("SET CONSTRAINTS complete_document_manifest IMMEDIATE"))
    wrong = create(qa)
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(
            text("UPDATE arm2_documents SET current_revision_id=:revision WHERE id=:id"),
            {"revision": current["id"], "id": wrong.split("/")[-1]},
        )
    assert bundle(c, url)["revisions"] == [current, previous]
    assert bundle(c, wrong)["revisions"] == []


def test_0065_preserves_real_0064_data_and_empty_schema_roundtrip(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        assert ScriptDirectory.from_config(config()).get_heads() == ["0070"]
        command.downgrade(config(), "0064")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO arm2_counterparties(id,name,status,version) VALUES('CP-000001','Прежний родитель','REVIEW',1)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO arm2_document_kinds(id,name,version,applicable_object_types) VALUES('DOC-KIND-000001','Прежний тип',2,'[\"CP\"]'::jsonb)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name,description,version) VALUES('DOCUMENT-000001','CP','CP-000001','DOC-KIND-000001','Прежний документ','сохранить',3)"
                )
            )
            db.execute(
                text("""INSERT INTO arm2_history_events(object_type,object_id,operation,actor_kind,actor_id,source,version_before,version_after,changes)
              VALUES('DOCUMENT','DOCUMENT-000001','UPDATED','HUMAN','1','0064-test',2,3,CAST(:changes AS jsonb))"""),
                {"changes": json.dumps({"version": {"before": 2, "after": 3}})},
            )

        def snapshot():
            with engine.connect() as db:
                return [
                    db.execute(text(sql)).all()
                    for sql in [
                        "SELECT id,name,version FROM arm2_document_kinds",
                        "SELECT id,parent_type,parent_id,kind_ref,name,description,version FROM arm2_documents",
                        "SELECT row_to_json(h)::text FROM arm2_history_events h",
                        "SELECT last_value,is_called FROM arm2_document_id_seq",
                    ]
                ]

        before = snapshot()
        command.upgrade(config(), "0065")
        assert snapshot() == before
        with engine.connect() as db:
            assert db.scalar(text("SELECT file_naming_template FROM arm2_document_kinds")) is None
            assert db.scalar(text("SELECT current_revision_id FROM arm2_documents")) is None
        command.downgrade(config(), "0064")
        assert snapshot() == before
        command.upgrade(config(), "0065")
        assert snapshot() == before
        assert len(inspect(engine).get_foreign_keys("arm2_document_revision_files")) == 2
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.mark.parametrize("reason", ["stage", "naming_history", "file_reference"])
def test_0065_guards_staging_naming_or_file_history_without_loss(
    isolated_arm2_database_url, monkeypatch, reason
):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        with engine.begin() as db:
            if reason == "stage":
                db.execute(
                    text(
                        "INSERT INTO users(id,login,name,password_hash,role,active,permissions) VALUES(999,'guard','guard','unused','OPERATOR',true,'[]'::jsonb)"
                    )
                )
                db.execute(
                    text(
                        "INSERT INTO arm2_counterparties(id,name,type,version) VALUES('CP-000001','Parent','Юридическое лицо',1)"
                    )
                )
                db.execute(
                    text(
                        "INSERT INTO arm2_document_kinds(id,name,applicable_object_types) VALUES('DOC-KIND-000001','Kind','[\"CP\"]'::jsonb)"
                    )
                )
                db.execute(
                    text(
                        "INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name) VALUES('DOCUMENT-000001','CP','CP-000001','DOC-KIND-000001','Document')"
                    )
                )
                db.execute(
                    text("""INSERT INTO arm2_document_file_stages(id,document_id,principal_id,session_hash,file_id,original_upload_name,size,state,expires_at)
                 VALUES(:id,'DOCUMENT-000001',999,:id,'FILE-000001','source.pdf',10,'empty',now()+interval '1 hour')"""),
                    {"id": "a" * 64},
                )
            else:
                changes = {"version": {"before": 1, "after": 2}}
                if reason == "naming_history":
                    changes["file_naming_template"] = {"before": None, "after": "DOCUMENT_NAME"}
                else:
                    changes["current_revision_id"] = {"before": None, "after": "DOCUMENT-REV-000001"}
                db.execute(
                    text("""INSERT INTO arm2_history_events(object_type,object_id,operation,actor_kind,actor_id,source,version_before,version_after,changes)
                  VALUES(:type,:id,'UPDATED','HUMAN','1','guard',1,2,CAST(:changes AS jsonb))"""),
                    {
                        "type": "DOC-KIND" if reason == "naming_history" else "DOCUMENT",
                        "id": "DOC-KIND-000001" if reason == "naming_history" else "DOCUMENT-000001",
                        "changes": json.dumps(changes),
                    },
                )
        with engine.connect() as db:
            before = db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h")).all()
        with pytest.raises(RuntimeError, match="downgrade refused"):
            command.downgrade(config(), "0064")
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0070"
            assert db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h")).all() == before
    finally:
        engine.dispose()
        settings.cache_clear()
