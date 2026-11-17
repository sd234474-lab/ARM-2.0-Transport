import json
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from app.arm2.document_kind_store import create_document_kind
from app.arm2.object_save import SaveContext
from app.config import settings
from app.models import User


def config():
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def snapshots(engine):
    with engine.connect() as db:
        return {
            "kinds": db.execute(text("SELECT id,name,version FROM arm2_document_kinds ORDER BY id")).all(),
            "history": db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id")).scalars().all(),
            "sequence": db.execute(text("SELECT last_value,is_called FROM arm2_document_kind_id_seq")).one(),
        }


def test_0062_existing_row_backfill_and_safe_roundtrip_preserve_old_data_and_events(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        command.downgrade(config(), "0062")
        with engine.begin() as db:
            assert db.scalar(text("SELECT nextval('arm2_document_kind_id_seq')")) == 1
            db.execute(text("INSERT INTO arm2_document_kinds (id,name,version) VALUES ('DOC-KIND-000001','Старый вид',1)"))
            db.execute(text("""INSERT INTO arm2_history_events
                (object_type,object_id,operation,actor_kind,actor_id,source,version_after,changes)
                VALUES ('DOC-KIND','DOC-KIND-000001','CREATED','HUMAN','1','old-0062',1,
                CAST(:changes AS jsonb))"""), {"changes": json.dumps({
                    "name": {"before": None, "after": "Старый вид"}, "version": {"before": None, "after": 1},
                })})
        old = snapshots(engine)
        command.upgrade(config(), "0063")
        assert snapshots(engine) == old
        columns = {c["name"]: c for c in inspect(engine).get_columns("arm2_document_kinds")}
        assert columns["applicable_object_types"]["default"] is None
        assert columns["applicable_object_types"]["nullable"] is False
        with engine.connect() as db:
            assert db.execute(text("SELECT applicable_object_types,max_current_documents FROM arm2_document_kinds")).one() == (["CP", "ORG", "VEH"], None)
        command.downgrade(config(), "0062")
        assert snapshots(engine) == old
        assert [c["name"] for c in inspect(engine).get_columns("arm2_document_kinds")] == ["id", "name", "version"]
        command.upgrade(config(), "0063")
        assert snapshots(engine) == old
        with Session(engine) as db:
            principal = User(login="backfill-editor", name="backfill-editor", password_hash="unused", role="OPERATOR",
                             active=True, permissions=["arm2_doc_kind_view", "arm2_doc_kind_edit"])
            db.add(principal)
            db.flush()
            new = create_document_kind(db, "Новый вид", applicable_object_types=["VEH"], max_current_documents=1,
                                       principal=principal, context=SaveContext("HUMAN", str(principal.id), "0063-test"))
            assert new.id == "DOC-KIND-000002"
            db.commit()
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.mark.parametrize("reason", ["parents", "maximum", "history"])
def test_0063_downgrade_refuses_settings_or_new_history_without_losses(isolated_arm2_database_url, monkeypatch, reason):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        with Session(engine) as db:
            principal = User(login="downgrade-editor", name="downgrade-editor", password_hash="unused", role="OPERATOR",
                             active=True, permissions=["arm2_doc_kind_view", "arm2_doc_kind_edit"])
            db.add(principal)
            db.flush()
            if reason == "history":
                create_document_kind(db, "With history", applicable_object_types=["CP", "ORG", "VEH"], max_current_documents=None,
                                     principal=principal, context=SaveContext("HUMAN", str(principal.id), "0063-test"))
            else:
                db.execute(text("INSERT INTO arm2_document_kinds (id,name,applicable_object_types,max_current_documents) "
                                "VALUES ('DOC-KIND-000001','Configured',CAST(:parents AS jsonb),:maximum)"),
                           {"parents": '["VEH"]' if reason == "parents" else '["CP","ORG","VEH"]',
                            "maximum": 1 if reason == "maximum" else None})
            db.commit()
        with engine.connect() as db:
            before = db.execute(text("SELECT row_to_json(d)::text FROM arm2_document_kinds d")).scalars().all()
        old = snapshots(engine)
        with pytest.raises(RuntimeError, match="settings/history would be lost"):
            command.downgrade(config(), "0062")
        assert snapshots(engine) == old
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
            assert db.execute(text("SELECT row_to_json(d)::text FROM arm2_document_kinds d")).scalars().all() == before
    finally:
        engine.dispose()
        settings.cache_clear()
