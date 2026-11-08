import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.history_models import HistoryEvent
from app.arm2.object_registry import (
    FieldSpec,
    ObjectDefinition,
    ObjectRegistry,
    ObjectRegistryError,
)
from app.arm2.object_save import SaveContext, create_object, update_object
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type

CONTEXT = SaveContext("SYSTEM", "core-test", "integration-test", "operation-001")


def test_type_uses_common_save_with_mandatory_atomic_history(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)

    with Session(engine) as db:
        vehicle_type = create_vehicle_type(
            db, "Самосвал", True, None, False, context=CONTEXT
        )
        db.commit()
        assert (vehicle_type.id, vehicle_type.version, vehicle_type.status) == (
            "TYPE-000001",
            1,
            "REVIEW",
        )

    with Session(engine) as db:
        updated = update_vehicle_type(
            db, vehicle_type.id, 1, "Самосвал", True, False, False, context=CONTEXT
        )
        db.commit()
        assert (updated.version, updated.status) == (2, "CONFIRMED")

    with Session(engine) as db:
        events = db.scalars(
            select(HistoryEvent).where(HistoryEvent.object_id == vehicle_type.id)
        ).all()
        assert len(events) == 2
        assert events[0].changes["requires_calibration"] == {"before": None, "after": None}
        assert events[0].changes["requires_wialon"] == {"before": None, "after": False}
        assert events[1].changes == {
            "requires_calibration": {"before": None, "after": False},
            "status": {"before": "REVIEW", "after": "CONFIRMED"},
            "version": {"before": 1, "after": 2},
        }

        no_op = update_vehicle_type(
            db, vehicle_type.id, 2, "Самосвал", True, False, False, context=CONTEXT
        )
        db.commit()
        assert no_op.version == 2
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 2

    def fail_history(*args, **kwargs):
        raise RuntimeError("history unavailable")

    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="history unavailable"):
            create_object(
                db,
                object_type="TYPE",
                values={
                    "name": "Без истории",
                    "requires_sts": True,
                    "requires_calibration": False,
                    "requires_wialon": False,
                },
                context=CONTEXT,
                history_writer=fail_history,
            )
        assert db.scalar(select(VehicleType).where(VehicleType.name == "Без истории")) is None
        db.commit()

    with Session(engine) as db:
        with pytest.raises(ObjectRegistryError, match="unknown: secret"):
            create_object(
                db,
                object_type="TYPE",
                values={
                    "name": "Опасное поле",
                    "requires_sts": True,
                    "requires_calibration": False,
                    "requires_wialon": False,
                    "secret": "token",
                },
                context=CONTEXT,
            )
        with pytest.raises(ObjectRegistryError, match="actor_id"):
            create_vehicle_type(
                db,
                "Нет автора",
                True,
                False,
                False,
                context=SaveContext("SYSTEM", " ", "integration-test"),
            )
        db.rollback()

    engine.dispose()


@dataclass
class FakeRecord:
    id: str
    name: str
    enabled: bool | None
    status: str
    version: int


class FakeSession:
    def begin_nested(self):
        return nullcontext()

    def flush(self):
        pass


class FakeAdapter:
    def __init__(self):
        self.records = {}
        self.sequence = 0

    def normalize(self, values):
        name = values["name"].strip()
        if not name:
            raise ValueError("fake name is required")
        enabled = values["enabled"]
        return {"name": name, "enabled": enabled, "status": "READY" if enabled is not None else "REVIEW"}

    def create_record(self, db, values):
        self.sequence += 1
        record = FakeRecord(f"FAKE-{self.sequence:06d}", version=1, **values)
        self.records[record.id] = record
        return record

    def lock_record(self, db, object_id):
        return self.records.get(object_id)

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


def test_second_isolated_object_reuses_the_same_save_algorithm():
    adapter = FakeAdapter()
    registry = ObjectRegistry()
    registry.register(
        ObjectDefinition(
            "FAKE",
            "FAKE",
            {"name": FieldSpec((str,)), "enabled": FieldSpec((bool,), nullable=True)},
            {
                "name": FieldSpec((str,)),
                "enabled": FieldSpec((bool,), nullable=True),
                "status": FieldSpec((str,)),
                "version": FieldSpec((int,)),
            },
            adapter=adapter,
        )
    )
    events = []

    def fake_history_writer(db, **event):
        events.append(event)
        return event

    session = FakeSession()
    created = create_object(
        session,
        object_type="FAKE",
        values={"name": "  Test object  ", "enabled": None},
        context=CONTEXT,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert (created.record.id, created.record.status, created.record.version) == (
        "FAKE-000001",
        "REVIEW",
        1,
    )
    updated = update_object(
        session,
        object_type="FAKE",
        object_id="FAKE-000001",
        expected_version=1,
        values={"name": "Test object", "enabled": False},
        context=CONTEXT,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert (updated.record.status, updated.record.version) == ("READY", 2)
    assert events[1]["changes"] == {
        "enabled": {"before": None, "after": False},
        "status": {"before": "REVIEW", "after": "READY"},
        "version": {"before": 1, "after": 2},
    }
    no_op = update_object(
        session,
        object_type="FAKE",
        object_id="FAKE-000001",
        expected_version=2,
        values={"name": "Test object", "enabled": False},
        context=CONTEXT,
        registry=registry,
        history_writer=fake_history_writer,
    )
    assert not no_op.changed
    assert len(events) == 2


def test_explicit_adapter_initialization_is_import_order_independent(database_url):
    script = r'''
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.arm2.object_save import SaveContext, create_object
from app.arm2.object_registry import (
    OBJECT_REGISTRY,
    ObjectRegistryError,
    initialize_object_adapters,
)

assert "app.arm2.vehicle_type_store" not in sys.modules
assert OBJECT_REGISTRY.require("TYPE").adapter is None
initialize_object_adapters()
adapter = OBJECT_REGISTRY.require("TYPE").adapter
assert adapter is not None
initialize_object_adapters()
assert OBJECT_REGISTRY.require("TYPE").adapter is adapter

engine = create_engine(os.environ["TEST_DATABASE_URL"])
with Session(engine) as db:
    result = create_object(
        db,
        object_type="TYPE",
        values={
            "name": "Изолированный TYPE",
            "requires_sts": True,
            "requires_calibration": False,
            "requires_wialon": False,
        },
        context=SaveContext("SYSTEM", "isolated-process", "integration-test"),
    )
    db.commit()
    assert result.record.id.startswith("TYPE-")
    assert result.event.object_id == result.record.id

with Session(engine) as db:
    from app.arm2.history_models import HistoryEvent
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) >= 1
    vehicle_adapter = OBJECT_REGISTRY.require("VEH").adapter
    assert vehicle_adapter is not None
    initialize_object_adapters()
    assert OBJECT_REGISTRY.require("VEH").adapter is vehicle_adapter
    vehicle = create_object(
        db,
        object_type="VEH",
        values={
            "name": "Изолированная машина",
            "plate_number": "А123ВС86",
            "type_id": None,
            "accounting_state": "Действующая",
            "model": None,
            "wialon_unit_id": None,
        },
        context=SaveContext("SYSTEM", "isolated-process", "integration-test"),
    )
    db.commit()
    assert vehicle.record.id.startswith("VEH-")
    assert vehicle.event.object_type == "VEH"
    assert vehicle.event.object_id == vehicle.record.id
engine.dispose()
'''
    environment = os.environ.copy()
    environment["TEST_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
