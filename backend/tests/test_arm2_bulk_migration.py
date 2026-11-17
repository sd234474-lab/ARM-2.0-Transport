from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.arm2.bulk_models import BulkCleanup, BulkReceipt, BulkSession
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type
from app.config import settings
from app.models import User


@pytest.fixture
def migrated(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    engine = create_engine(isolated_arm2_database_url)
    with Session(engine, expire_on_commit=False) as db:
        user = User(login="bulk-migration", name="migration", role="ADMIN_OPERATOR", active=True, password_hash="unused")
        db.add(user)
        db.flush()
        context = SaveContext("HUMAN", str(user.id), "migration-fixture")
        vehicle = create_vehicle(db, "Сохранить", "А567ВС86", principal=user, context=context)
        db.commit()
        yield engine, config, db, user, vehicle, context
    engine.dispose()


def snapshot(engine):
    with engine.connect() as db:
        return {table: db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY 1")).scalars().all()
                for table in ("arm2_vehicles", "arm2_history_events", "users")}


def test_empty_bulk_schema_roundtrip_preserves_vehicle_history_and_sequence(migrated):
    engine, config, db, _, vehicle, _ = migrated
    before = snapshot(engine)
    db.rollback()
    command.downgrade(config, "0065")
    command.upgrade(config, "0066")
    assert snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
        assert conn.exec_driver_sql("SELECT last_value,is_called FROM arm2_vehicle_id_seq").one() == (1, True)
    assert db.get(Vehicle, vehicle.id).id == vehicle.id


@pytest.mark.parametrize("existing", ["workspace", "receipt", "cleanup"])
def test_bulk_downgrade_never_discards_workspace_protocol_or_cleanup_audit(migrated, existing):
    engine, config, db, user, vehicle, _ = migrated
    sid = str(uuid4())
    if existing == "workspace":
        db.add(BulkSession(id=sid, user_id=user.id, source="test.tsv", revision=1, payload={"rows": []}))
    elif existing == "receipt":
        event = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == vehicle.id))
        db.add(BulkReceipt(command_id=str(uuid4()), session_id=sid, user_id=user.id, source="test.tsv",
                           fingerprint="a"*64, row_key=vehicle.id, vehicle_id=vehicle.id, event_id=event.event_id,
                           version_after=1, fields=["name"]))
    else:
        db.add(BulkCleanup(id=str(uuid4()), session_id=sid, user_id=user.id))
    db.commit()
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="B4.*downgrade refused"):
        command.downgrade(config, "0065")
    assert snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
        table = {"workspace": "arm2_bulk_sessions", "receipt": "arm2_bulk_receipts", "cleanup": "arm2_bulk_cleanup"}[existing]
        assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 1


def test_type_category_history_blocks_lossy_downgrade_even_after_clear(migrated):
    engine, config, db, user, _, context = migrated
    typ = create_vehicle_type(db, "Категория", True, False, False, plate_category="Автомобильный", principal=user, context=context)
    db.commit()
    update_vehicle_type(db, typ.id, 1, typ.name, True, False, False, plate_category=None, principal=user, context=context)
    db.commit()
    before = snapshot(engine)
    with pytest.raises(RuntimeError, match="TYPE category data/history"):
        command.downgrade(config, "0064")
    assert snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
