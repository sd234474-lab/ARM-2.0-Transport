from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import (
    InvalidExpectedVersionError,
    VehicleTypeNotFoundError,
    VehicleTypeVersionConflictError,
    create_vehicle_type,
    update_vehicle_type,
)


def test_vehicle_type_update_is_version_safe_and_transactional(database_url):
    engine = create_engine(database_url, poolclass=NullPool)

    with Session(engine) as db:
        vehicle_type = create_vehicle_type(db, "Самосвал", requires_wialon=True)
        db.commit()
        type_id = vehicle_type.id
    assert type_id == "TYPE-000001"

    barrier = Barrier(2)

    def concurrent_update(name):
        with Session(engine) as db:
            barrier.wait()
            try:
                updated = update_vehicle_type(db, type_id, 1, name, True, False, False)
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
        current = db.get(VehicleType, type_id)
        assert current is not None
        assert (current.id, current.name, current.status, current.version) == (
            type_id,
            winning_name,
            "CONFIRMED",
            2,
        )
        with pytest.raises(VehicleTypeVersionConflictError):
            update_vehicle_type(db, type_id, 1, "Устаревшая правка", True, True, True)
        db.rollback()

    with Session(engine) as db:
        temporary = update_vehicle_type(db, type_id, 2, "Временное имя", True, True, True)
        assert (temporary.id, temporary.version) == (type_id, 3)
        with Session(engine) as observer:
            visible = observer.get(VehicleType, type_id)
            assert visible is not None
            assert (visible.name, visible.version) == (winning_name, 2)
        db.rollback()

    with Session(engine) as db:
        updated = update_vehicle_type(db, type_id, 2, "Обновлённый самосвал", True, False, False)
        db.commit()
        assert (updated.id, updated.status, updated.version) == (type_id, "CONFIRMED", 3)

    with Session(engine) as db:
        unchanged = update_vehicle_type(db, type_id, 3, "Обновлённый самосвал", True, False, False)
        db.commit()
        assert unchanged.version == 3

    with Session(engine) as db:
        review = update_vehicle_type(db, type_id, 3, "Обновлённый самосвал", True, False, None)
        db.commit()
        assert (review.status, review.version) == ("REVIEW", 4)

    with Session(engine) as db:
        confirmed = update_vehicle_type(db, type_id, 4, "Обновлённый самосвал", True, False, False)
        db.commit()
        assert (confirmed.status, confirmed.version) == ("CONFIRMED", 5)

    with Session(engine) as db:
        with pytest.raises(ValueError, match="requires_sts must be True"):
            update_vehicle_type(db, type_id, 5, "Недопустимый СТС", False, True, True)
        db.rollback()
        with pytest.raises(ValueError, match="must not be empty"):
            update_vehicle_type(db, type_id, 5, "   ", True, True, True)
        db.rollback()

    with Session(engine) as db:
        other = create_vehicle_type(db, "Погрузчик", True, True, True)
        db.commit()
        assert other.id == "TYPE-000002"

    with Session(engine) as db:
        with pytest.raises(IntegrityError):
            update_vehicle_type(db, type_id, 5, "  Погрузчик  ", True, True, True)
        db.rollback()
        preserved = db.get(VehicleType, type_id)
        assert preserved is not None
        assert (preserved.id, preserved.name, preserved.version) == (
            type_id,
            "Обновлённый самосвал",
            5,
        )

    with Session(engine) as db:
        with pytest.raises(VehicleTypeNotFoundError):
            update_vehicle_type(db, "TYPE-999999", 1, "Нет записи", True, True, True)
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
    engine.dispose()
