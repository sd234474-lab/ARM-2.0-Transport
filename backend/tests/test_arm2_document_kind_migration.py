from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from app.arm2.document_kind_store import create_document_kind
from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import append_history_event
from app.arm2.ids import next_id
from app.arm2.object_save import SaveContext
from app.config import settings
from app.models import User


def configuration():
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def snapshot(engine):
    tables = ["users", "arm2_vehicle_types", "arm2_vehicles", "arm2_counterparties", "arm2_organizations", "arm2_history_events"]
    with engine.connect() as connection:
        return {table: sorted(connection.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()) for table in tables}


def frozen_legacy_record(db, kind, table, values, principal):
    """Seed the 0064 representation, independently of newer TYPE commands."""
    identifier = next_id(db, kind)
    fields = {"id": identifier, **values, "version": 1}
    columns = ",".join(fields)
    placeholders = ",".join(":" + key for key in fields)
    db.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), fields)
    db.add(HistoryEvent(object_type=kind, object_id=identifier, operation="CREATED", actor_kind="HUMAN",
                        actor_id=str(principal.id), source="migration-regression", version_after=1,
                        changes={k: {"before": None, "after": v} for k, v in fields.items() if k != "id"},
                        related_objects=[]))
    db.flush()
    return SimpleNamespace(id=identifier)


def test_upgrade_downgrade_roundtrip_preserves_legacy_objects_history_and_ids(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        assert ScriptDirectory.from_config(configuration()).get_heads() == ["0072"]
        # This test exercises the historical B3 migration boundary. New TYPE
        # categories and their guarded downgrade have their own B4 tests.
        command.downgrade(configuration(), "0064")
        with Session(engine) as db:
            principal = User(login="legacy-regression", name="legacy", password_hash="unused", active=True,
                             role="ADMIN_OPERATOR", permissions=[])
            db.add(principal)
            db.flush()
            ctx = SaveContext("HUMAN", str(principal.id), "migration-regression")
            kind = frozen_legacy_record(db, "TYPE", "arm2_vehicle_types", {"name": "Старый тип", "requires_sts": True,
                                        "requires_calibration": False, "requires_wialon": False, "status": "CONFIRMED"}, principal)
            cp = frozen_legacy_record(db, "CP", "arm2_counterparties", {"name": "Старый CP", "status": "REVIEW"}, principal)
            org = frozen_legacy_record(db, "ORG", "arm2_organizations", {"name": "Старая ORG", "status": "REVIEW"}, principal)
            vehicle = frozen_legacy_record(db, "VEH", "arm2_vehicles", {"name": "Старая техника", "plate_number": "А123ВС86",
                                           "type_id": kind.id, "owner_cp_id": cp.id, "owner_org_id": None, "model": None,
                                           "wialon_unit_id": None, "accounting_state": "Действующая"}, principal)
            assert (kind.id, cp.id, org.id, vehicle.id) == ("TYPE-000001", "CP-000001", "ORG-000001", "VEH-000001")
            db.commit()
        before = snapshot(engine)
        command.downgrade(configuration(), "0061")
        assert "arm2_document_kinds" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('arm2_document_kind_id_seq')")) is None
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0061"
        assert snapshot(engine) == before
        command.upgrade(configuration(), "0063")
        assert snapshot(engine) == before
        assert [c["name"] for c in inspect(engine).get_columns("arm2_document_kinds")] == [
            "id", "name", "version", "applicable_object_types", "max_current_documents",
        ]
        command.upgrade(configuration(), "0065")
        with Session(engine) as db:
            principal = db.scalar(select(User).where(User.login == "legacy-regression"))
            ctx = SaveContext("HUMAN", str(principal.id), "migration-regression")
            new = create_document_kind(db, "Новый тип документа", applicable_object_types=["VEH"], max_current_documents=1,
                                       principal=principal, context=ctx)
            assert new.id == "DOC-KIND-000001"
            # Independent sequences and pre-existing legacy data survived the roundtrip.
            next_type = frozen_legacy_record(db, "TYPE", "arm2_vehicle_types", {"name": "Следующий старый тип",
                                            "requires_sts": True, "requires_calibration": None, "requires_wialon": None,
                                            "status": "REVIEW"}, principal)
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
                conn.execute(text("INSERT INTO arm2_document_kinds (id,name,applicable_object_types) "
                                  "VALUES ('DOC-KIND-000001','Сохранить','[\"CP\",\"ORG\",\"VEH\"]'::jsonb)"))
        if existing != "row":
            with Session(engine) as db:
                if existing == "event":
                    append_history_event(
                        db, object_type="DOC-KIND", object_id="DOC-KIND-000001", operation="CREATED",
                        actor_kind="HUMAN", actor_id="1", source="migration-test", version_after=1,
                        changes={"name": {"before": None, "after": "Сохранить"}, "version": {"before": None, "after": 1},
                                 "applicable_object_types": {"before": None, "after": ["CP", "ORG", "VEH"]},
                                 "max_current_documents": {"before": None, "after": None}, "file_naming_template": {"before": None, "after": None}},
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
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0072"
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
