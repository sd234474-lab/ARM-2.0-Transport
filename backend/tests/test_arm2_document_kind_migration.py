from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from app.arm2.counterparty_store import create_counterparty
from app.arm2.document_kind_store import create_document_kind
from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import append_history_event
from app.arm2.object_save import SaveContext
from app.arm2.organization_store import create_organization
from app.arm2.vehicle_store import create_vehicle
from app.arm2.vehicle_type_store import create_vehicle_type
from app.config import settings
from app.models import User


def configuration():
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def snapshot(engine):
    tables = ["users", "arm2_vehicle_types", "arm2_vehicles", "arm2_counterparties", "arm2_organizations", "arm2_history_events"]
    with engine.connect() as connection:
        return {table: sorted(connection.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()) for table in tables}


def test_upgrade_downgrade_roundtrip_preserves_legacy_objects_history_and_ids(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        assert ScriptDirectory.from_config(configuration()).get_heads() == ["0062"]
        with Session(engine) as db:
            principal = User(login="legacy-regression", name="legacy", password_hash="unused", active=True,
                             role="ADMIN_OPERATOR", permissions=[])
            db.add(principal)
            db.flush()
            ctx = SaveContext("HUMAN", str(principal.id), "migration-regression")
            kind = create_vehicle_type(db, "Старый тип", requires_calibration=False, requires_wialon=False,
                                       principal=principal, context=ctx)
            cp = create_counterparty(db, "Старый CP", principal=principal, context=ctx)
            org = create_organization(db, "Старая ORG", principal=principal, context=ctx)
            vehicle = create_vehicle(db, "Старая техника", "А123ВС86", type_id=kind.id,
                                     owner_cp_id=cp.id, principal=principal, context=ctx)
            assert (kind.id, cp.id, org.id, vehicle.id) == ("TYPE-000001", "CP-000001", "ORG-000001", "VEH-000001")
            db.commit()
        before = snapshot(engine)
        command.downgrade(configuration(), "0061")
        assert "arm2_document_kinds" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('arm2_document_kind_id_seq')")) is None
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0061"
        assert snapshot(engine) == before
        command.upgrade(configuration(), "0062")
        assert snapshot(engine) == before
        assert [c["name"] for c in inspect(engine).get_columns("arm2_document_kinds")] == ["id", "name", "version"]
        with Session(engine) as db:
            principal = db.scalar(select(User).where(User.login == "legacy-regression"))
            ctx = SaveContext("HUMAN", str(principal.id), "migration-regression")
            new = create_document_kind(db, "Новый тип документа", principal=principal, context=ctx)
            assert new.id == "DOC-KIND-000001"
            # Independent sequences and pre-existing legacy data survived the roundtrip.
            next_type = create_vehicle_type(db, "Следующий старый тип", principal=principal, context=ctx)
            assert next_type.id == "TYPE-000002"
            db.commit()
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.mark.parametrize("existing", ["row", "event", "reference"])
def test_downgrade_refuses_existing_document_kind_data_without_deleting_it(isolated_arm2_database_url, monkeypatch, existing):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            if existing == "row":
                conn.execute(text("INSERT INTO arm2_document_kinds (id,name) VALUES ('DOC-KIND-000001','Сохранить')"))
        if existing != "row":
            with Session(engine) as db:
                if existing == "event":
                    append_history_event(
                        db, object_type="DOC-KIND", object_id="DOC-KIND-000001", operation="CREATED",
                        actor_kind="HUMAN", actor_id="1", source="migration-test", version_after=1,
                        changes={"name": {"before": None, "after": "Сохранить"}, "version": {"before": None, "after": 1}},
                    )
                else:
                    append_history_event(
                        db, object_type="VEH", object_id="VEH-000001", operation="UPDATED", actor_kind="HUMAN",
                        actor_id="1", source="migration-test", version_before=1, version_after=2,
                        changes={"version": {"before": 1, "after": 2}},
                        related_objects=[{"object_type": "DOC-KIND", "object_id": "DOC-KIND-000001", "relation": "test"}],
                    )
                db.commit()
        before = snapshot(engine)
        with pytest.raises(RuntimeError, match="downgrade refused without deleting data"):
            command.downgrade(configuration(), "0061")
        assert snapshot(engine) == before
        assert "arm2_document_kinds" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0062"
            if existing == "row":
                assert conn.scalar(text("SELECT name FROM arm2_document_kinds")) == "Сохранить"
    finally:
        engine.dispose()
        settings.cache_clear()


@pytest.mark.parametrize("kind_id", ["TYPE-000001", "DOC-KIND-00001", "DOC-KIND-ABCDEF"])
def test_history_db_check_rejects_mismatched_doc_kind_ids(isolated_arm2_database_url, kind_id):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        with Session(engine) as db, pytest.raises(IntegrityError):
            db.add(HistoryEvent(
                object_type="DOC-KIND", object_id=kind_id, operation="CREATED", actor_kind="HUMAN",
                actor_id="1", source="migration-test", version_after=1,
                changes={"name": {"before": None, "after": "Test"}, "version": {"before": None, "after": 1}},
                related_objects=[],
            ))
            db.flush()
    finally:
        engine.dispose()
