import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import ObjectVersionConflictError, SaveContext, create_object
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle, update_vehicle
from app.arm2.vehicle_type_store import create_vehicle_type
from app.models import User


def add_admin(db):
    user = User(
        login="veh-core-admin",
        name="veh-core-admin",
        password_hash="unused",
        role="ADMIN_OPERATOR",
        active=True,
        permissions=[],
    )
    db.add(user)
    db.flush()
    return user


def save_context(user):
    return SaveContext("HUMAN", str(user.id), "integration-test")


def test_veh_uses_same_pipeline_as_type_with_history_and_versions(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        principal = add_admin(db)
        confirmed = create_vehicle_type(
            db, "Самосвал", True, False, False,
            principal=principal, context=save_context(principal)
        )
        incomplete = create_vehicle_type(
            db, "Незавершённый", True, None, False,
            principal=principal, context=save_context(principal)
        )
        vehicle = create_vehicle(
            db, "  Машина 1  ", "A123BC86", type_id=confirmed.id,
            principal=principal, context=save_context(principal)
        )
        assert (vehicle.id, vehicle.name, vehicle.plate_number, vehicle.version) == (
            "VEH-000001", "Машина 1", "А123ВС86", 1
        )
        assert vehicle.accounting_state == "Действующая"
        db.commit()
        principal_id = principal.id
        vehicle_id = vehicle.id
        confirmed_id = confirmed.id
        incomplete_id = incomplete.id
        with pytest.raises(ValueError, match="CONFIRMED"):
            create_vehicle(
                db, "Нельзя назначить", "B123BC86", type_id=incomplete_id,
                principal=principal, context=save_context(principal)
            )
        with pytest.raises(ValueError, match="CONFIRMED"):
            create_vehicle(
                db,
                "Несуществующий тип",
                "B124BC86",
                type_id="TYPE-999999",
                principal=principal,
                context=save_context(principal),
            )
        db.rollback()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        vehicle = update_vehicle(
            db, vehicle_id, 1, name="Машина 1", plate_number="А123ВС86",
            type_id=None, accounting_state="Архивная", model=" Модель ", wialon_unit_id=None,
            principal=principal, context=save_context(principal),
        )
        db.commit()
        assert (vehicle.id, vehicle.version, vehicle.model, vehicle.accounting_state) == (
            vehicle_id, 2, "Модель", "Архивная"
        )
        unchanged = update_vehicle(
            db, vehicle_id, 2, name="Машина 1", plate_number="А123ВС86",
            type_id=None, accounting_state="Архивная", model="Модель", wialon_unit_id=None,
            principal=principal, context=save_context(principal),
        )
        assert unchanged.version == 2
        db.commit()
        with pytest.raises(ObjectVersionConflictError):
            update_vehicle(
                db, vehicle_id, 1, name="Другое", plate_number="А123ВС86",
                type_id=None, accounting_state="Архивная", model="Модель", wialon_unit_id=None,
                principal=principal, context=save_context(principal),
            )
        db.rollback()

    with Session(engine) as db:
        events = db.scalars(
            select(HistoryEvent).where(
                HistoryEvent.object_type == "VEH", HistoryEvent.object_id == vehicle_id
            ).order_by(HistoryEvent.event_id)
        ).all()
        assert len(events) == 2
        assert events[0].operation == "CREATED"
        assert events[0].related_objects == [
            {
                "object_type": "TYPE",
                "object_id": confirmed_id,
                "relation": "vehicle_type",
            }
        ]
        assert events[1].changes["accounting_state"] == {
            "before": "Действующая", "after": "Архивная"
        }
        assert events[1].changes["type_id"]["before"] == confirmed_id
        assert events[1].changes["type_id"]["after"] is None
        assert db.scalar(select(func.count()).select_from(Vehicle)) == 1
    engine.dispose()


def test_veh_input_and_history_failure_are_atomic(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        principal = add_admin(db)
        db.commit()
        with pytest.raises(ValueError, match="plate_number"):
            create_vehicle(
                db, "Брак", "INVALID",
                principal=principal, context=save_context(principal)
            )
        with pytest.raises(ValueError, match="plate_number"):
            create_vehicle(
                db, "Брак", "А123 ВС86",
                principal=principal, context=save_context(principal)
            )
        db.rollback()
        first = create_vehicle(
            db, "Один", "5939EE86",
            principal=principal, context=save_context(principal)
        )
        db.commit()
        principal_id = principal.id
        assert (first.id, first.plate_number) == ("VEH-000001", "5939ЕЕ86")
        with pytest.raises(IntegrityError):
            create_vehicle(
                db, "Дубликат", "5939ЕЕ86",
                principal=principal, context=save_context(principal)
            )
        db.rollback()

    def broken_history(*args, **kwargs):
        raise RuntimeError("history unavailable")

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(RuntimeError, match="history unavailable"):
            create_object(
                db, object_type="VEH",
                values={
                    "name": "Не сохранять", "plate_number": "А123ВС86", "type_id": None,
                    "accounting_state": "Действующая", "model": None, "wialon_unit_id": None,
                        "owner_cp_id": None, "owner_org_id": None,
                },
                principal=principal,
                context=save_context(principal),
                history_writer=broken_history,
            )
        assert db.scalar(select(Vehicle).where(Vehicle.name == "Не сохранять")) is None
        db.commit()
    engine.dispose()
