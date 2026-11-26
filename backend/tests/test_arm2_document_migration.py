from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from app.arm2.history_store import append_history_event
from app.config import settings


def test_0064_roundtrip_typed_parent_fks_and_loss_guard(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    try:
        command.downgrade(cfg, "0063")
        with engine.connect() as db:
            before = (
                db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id"))
                .scalars()
                .all()
            )
        command.upgrade(cfg, "0064")
        with engine.connect() as db:
            assert (
                db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id"))
                .scalars()
                .all()
                == before
            )
        assert len(inspect(engine).get_foreign_keys("arm2_documents")) == 4
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO arm2_document_kinds(id,name,applicable_object_types) VALUES('DOC-KIND-000001','Миграция','[\"CP\"]'::jsonb)"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name) VALUES('DOCUMENT-000001','CP','CP-000001','DOC-KIND-000001','Без родителя')"
                )
            )
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO arm2_counterparties(id,name,status,version) VALUES('CP-000001','Родитель','REVIEW',1)"
                )
            )
            db.execute(
                text(
                    "INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name) VALUES('DOCUMENT-000001','CP','CP-000001','DOC-KIND-000001','Сохранить')"
                )
            )
        for query in [
            "DELETE FROM arm2_counterparties WHERE id='CP-000001'",
            "DELETE FROM arm2_document_kinds WHERE id='DOC-KIND-000001'",
        ]:
            with pytest.raises(IntegrityError), engine.begin() as db:
                db.execute(text(query))
        with pytest.raises(RuntimeError, match="downgrade refused"):
            command.downgrade(cfg, "0063")
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0064"
            assert db.scalar(text("SELECT name FROM arm2_documents")) == "Сохранить"
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.mark.parametrize("related_only", [False, True])
@pytest.mark.parametrize("isolated_arm2_database_url", ["0064"], indirect=True)
def test_0064_refuses_history_without_document_and_preserves_it(
    isolated_arm2_database_url, monkeypatch, related_only
):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    try:
        with Session(engine) as db:
            append_history_event(
                db,
                object_type="VEH" if related_only else "DOCUMENT",
                object_id="VEH-000001" if related_only else "DOCUMENT-000001",
                operation="UPDATED",
                actor_kind="HUMAN",
                actor_id="1",
                source="migration-test",
                version_before=1,
                version_after=2,
                changes={"version": {"before": 1, "after": 2}},
                related_objects=[
                    {"object_type": "DOCUMENT", "object_id": "DOCUMENT-000001", "relation": "test"}
                ]
                if related_only
                else [],
            )
            db.commit()
        with engine.connect() as db:
            before = (
                db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id"))
                .scalars()
                .all()
            )
        with pytest.raises(RuntimeError, match="DOCUMENT data/history exists"):
            command.downgrade(cfg, "0063")
        with engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0064"
            assert (
                db.execute(text("SELECT row_to_json(h)::text FROM arm2_history_events h ORDER BY event_id"))
                .scalars()
                .all()
                == before
            )
            assert db.scalar(text("SELECT count(*) FROM arm2_documents")) == 0
    finally:
        engine.dispose()
        settings.cache_clear()
