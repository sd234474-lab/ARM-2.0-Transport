import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import HistoryValidationError, append_history_event
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type


def created_changes(name, requires_calibration, requires_wialon, status):
    values = {
        "name": name,
        "requires_sts": True,
        "requires_calibration": requires_calibration,
        "requires_wialon": requires_wialon,
        "status": status,
        "version": 1,
    }
    return {field: {"before": None, "after": value} for field, value in values.items()}


def test_shared_history_is_detailed_filtered_atomic_and_safe(database_url):
    engine = create_engine(database_url, poolclass=NullPool)

    with Session(engine) as db:
        vehicle_type = create_vehicle_type(db, "Самосвал", True, None, False)
        created = append_history_event(
            db,
            object_type="TYPE",
            object_id=vehicle_type.id,
            operation="CREATED",
            actor_kind="SYSTEM",
            actor_id="arm2-history-test",
            source="integration-test",
            correlation_id="batch-001",
            version_after=1,
            changes=created_changes("Самосвал", None, False, "REVIEW"),
        )
        assert created.event_id is not None
        db.commit()
        first_id = vehicle_type.id

    with Session(engine) as db:
        created = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == first_id))
        assert created is not None
        assert created.changes["requires_calibration"] == {"before": None, "after": None}
        assert created.changes["requires_wialon"] == {"before": None, "after": False}
        assert created.version_after == 1
        assert created.occurred_at.tzinfo is not None

        vehicle_type = update_vehicle_type(db, first_id, 1, "Самосвал", True, False, False)
        append_history_event(
            db,
            object_type="TYPE",
            object_id=first_id,
            operation="UPDATED",
            actor_kind="HUMAN",
            actor_id="operator-001",
            source="internal-service",
            version_before=1,
            version_after=2,
            changes={
                "requires_calibration": {"before": None, "after": False},
                "status": {"before": "REVIEW", "after": "CONFIRMED"},
                "version": {"before": 1, "after": 2},
            },
        )
        assert vehicle_type.version == 2
        db.commit()

    with Session(engine) as db:
        other = create_vehicle_type(db, "Погрузчик", True, False, False)
        append_history_event(
            db,
            object_type="TYPE",
            object_id=other.id,
            operation="CREATED",
            actor_kind="SYSTEM",
            actor_id="arm2-history-test",
            source="integration-test",
            version_after=1,
            changes=created_changes("Погрузчик", False, False, "CONFIRMED"),
        )
        db.commit()
        other_id = other.id

    with Session(engine) as db:
        events = db.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.object_type == "TYPE", HistoryEvent.object_id == first_id)
            .order_by(HistoryEvent.occurred_at, HistoryEvent.event_id)
        ).all()
        assert [(event.operation, event.version_after) for event in events] == [
            ("CREATED", 1),
            ("UPDATED", 2),
        ]
        assert all(event.object_id != other_id for event in events)

    with Session(engine) as db:
        rolled_back = create_vehicle_type(db, "Будет отменён", True, False, False)
        append_history_event(
            db,
            object_type="TYPE",
            object_id=rolled_back.id,
            operation="CREATED",
            actor_kind="SYSTEM",
            actor_id="rollback-test",
            source="integration-test",
            version_after=1,
            changes=created_changes("Будет отменён", False, False, "CONFIRMED"),
        )
        rolled_back_id = rolled_back.id
        db.rollback()

    with Session(engine) as db:
        assert db.get(VehicleType, rolled_back_id) is None
        assert db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == rolled_back_id)) is None

    valid_changes = created_changes("Валидация", False, False, "CONFIRMED")
    invalid_events = [
        {"object_type": "OTHER", "object_id": "OTHER-000001", "changes": valid_changes},
        {"object_type": "TYPE", "object_id": "VEH-000001", "changes": valid_changes},
        {"object_type": "TYPE", "object_id": "TYPE-900001", "actor_id": " ", "changes": valid_changes},
        {"object_type": "TYPE", "object_id": "TYPE-900001", "source": "", "changes": valid_changes},
        {
            "object_type": "TYPE",
            "object_id": "TYPE-900001",
            "changes": {**valid_changes, "secret": {"before": None, "after": "value"}},
        },
        {
            "object_type": "TYPE",
            "object_id": "TYPE-900001",
            "changes": {**valid_changes, "name": {"before": None, "after": {"nested": True}}},
        },
        {
            "object_type": "TYPE",
            "object_id": "TYPE-900001",
            "changes": {**valid_changes, "name": {"before": None, "after": b"bytes"}},
        },
        {
            "object_type": "VEH",
            "object_id": "VEH-000001",
            "changes": {"name": {"before": None, "after": "Not registered"}},
        },
    ]
    for invalid in invalid_events:
        payload = {
            "object_type": "TYPE",
            "object_id": "TYPE-900001",
            "operation": "CREATED",
            "actor_kind": "SYSTEM",
            "actor_id": "validation-test",
            "source": "integration-test",
            "version_after": 1,
            "changes": valid_changes,
            **invalid,
        }
        with Session(engine) as db:
            with pytest.raises(HistoryValidationError):
                append_history_event(db, **payload)
            db.rollback()

    inspector = inspect(engine)
    assert not any(table.startswith("nsi_") for table in inspector.get_table_names())
    index = next(
        item
        for item in inspector.get_indexes("arm2_history_events")
        if item["name"] == "ix_arm2_history_object_chronology"
    )
    assert index["column_names"] == ["object_type", "object_id", "occurred_at", "event_id"]
    engine.dispose()
