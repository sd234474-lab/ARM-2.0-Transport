from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import (
    InvalidExpectedVersionError,
    VehicleTypeNotFoundError,
    VehicleTypeVersionConflictError,
    create_vehicle_type,
    update_vehicle_type,
)
from app.models import User


def save_context(user):
    return SaveContext("HUMAN", str(user.id), "integration-test")


def test_vehicle_type_update_is_version_safe_and_transactional(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)

    with Session(engine) as db:
        principal = User(
            login="type-update-admin",
            name="type-update-admin",
            password_hash="unused",
            role="ADMIN_OPERATOR",
            active=True,
            permissions=[],
        )
        db.add(principal)
        db.flush()
        vehicle_type = create_vehicle_type(
            db, "Самосвал", requires_wialon=True,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        type_id = vehicle_type.id
        principal_id = principal.id
    assert type_id == "TYPE-000001"

    barrier = Barrier(2)

    def concurrent_update(name):
        with Session(engine) as db:
            principal = db.get(User, principal_id)
            barrier.wait()
            try:
                updated = update_vehicle_type(
                    db, type_id, 1, name, True, False, False,
                    principal=principal, context=save_context(principal)
                )
                db.commit()
                return "updated", updated.name, updated.version
            except VehicleTypeVersionConflictError:
                db.rollback()
                return "conflict", None, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(concurrent_update, ("Самосвал А", "Самосвал Б")))
    assert sorted(result[0] for result in results) == ["conflict", "updated"]
    winning_name = next(result[1] for result in results if result[0] == "updated")

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        current = db.get(VehicleType, type_id)
        assert current is not None
        assert (current.id, current.name, current.status, current.version) == (
            type_id,
            winning_name,
            "CONFIRMED",
            2,
        )
        with pytest.raises(VehicleTypeVersionConflictError):
            update_vehicle_type(
                db, type_id, 1, "Устаревшая правка", True, True, True,
                principal=principal, context=save_context(principal)
            )
        db.rollback()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        temporary = update_vehicle_type(
            db, type_id, 2, "Временное имя", True, True, True,
            principal=principal, context=save_context(principal)
        )
        assert (temporary.id, temporary.version) == (type_id, 3)
        with Session(engine) as observer:
            visible = observer.get(VehicleType, type_id)
            assert visible is not None
            assert (visible.name, visible.version) == (winning_name, 2)
        db.rollback()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        updated = update_vehicle_type(
            db, type_id, 2, "Обновлённый самосвал", True, False, False,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert (updated.id, updated.status, updated.version) == (type_id, "CONFIRMED", 3)

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        unchanged = update_vehicle_type(
            db, type_id, 3, "Обновлённый самосвал", True, False, False,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert unchanged.version == 3

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        review = update_vehicle_type(
            db, type_id, 3, "Обновлённый самосвал", True, False, None,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert (review.status, review.version) == ("REVIEW", 4)

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        confirmed = update_vehicle_type(
            db, type_id, 4, "Обновлённый самосвал", True, False, False,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert (confirmed.status, confirmed.version) == ("CONFIRMED", 5)

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(ValueError, match="requires_sts must be True"):
            update_vehicle_type(
                db, type_id, 5, "Недопустимый СТС", False, True, True,
                principal=principal, context=save_context(principal)
            )
        db.rollback()
        with pytest.raises(ValueError, match="must not be empty"):
            update_vehicle_type(
                db, type_id, 5, "   ", True, True, True,
                principal=principal, context=save_context(principal)
            )
        db.rollback()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        other = create_vehicle_type(
            db, "Погрузчик", True, True, True,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert other.id == "TYPE-000002"

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(IntegrityError):
            update_vehicle_type(
                db, type_id, 5, "  Погрузчик  ", True, True, True,
                principal=principal, context=save_context(principal)
            )
        db.rollback()
        preserved = db.get(VehicleType, type_id)
        assert preserved is not None
        assert (preserved.id, preserved.name, preserved.version) == (
            type_id,
            "Обновлённый самосвал",
            5,
        )

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(VehicleTypeNotFoundError):
            update_vehicle_type(
                db, "TYPE-999999", 1, "Нет записи", True, True, True,
                principal=principal, context=save_context(principal)
            )
        for invalid_version in (0, -1, True, 1.0):
            with pytest.raises(InvalidExpectedVersionError):
                update_vehicle_type(
                    db,
                    type_id,
                    invalid_version,
                    "Неважно",
                    True,
                    True,
                    True,
                    principal=principal,
                    context=save_context(principal),
                )
        db.rollback()

    with engine.connect() as connection:
        type_sequence = connection.exec_driver_sql(
            "SELECT last_value, is_called FROM arm2_vehicle_type_id_seq"
        ).one()
    assert type_sequence == (2, True)
    with Session(engine) as db:
        rows = db.scalars(select(VehicleType).order_by(VehicleType.id)).all()
        assert [(row.id, row.version) for row in rows] == [
            ("TYPE-000001", 5),
            ("TYPE-000002", 1),
        ]
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 6
    engine.dispose()
