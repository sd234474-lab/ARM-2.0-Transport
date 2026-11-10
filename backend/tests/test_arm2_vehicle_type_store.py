from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.object_save import SaveContext
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type
from app.models import User


def save_context(user):
    return SaveContext("HUMAN", str(user.id), "integration-test")


def test_vehicle_type_storage_invariants_and_ids(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)

    with Session(engine) as db:
        principal = User(
            login="type-store-admin",
            name="type-store-admin",
            password_hash="unused",
            role="ADMIN_OPERATOR",
            active=True,
            permissions=[],
        )
        db.add(principal)
        db.flush()
        review = create_vehicle_type(
            db, "  Самосвал  ", requires_wialon=True,
            principal=principal, context=save_context(principal)
        )
        confirmed = create_vehicle_type(
            db,
            "Погрузчик",
            requires_calibration=False,
            requires_wialon=False,
            principal=principal,
            context=save_context(principal),
        )
        db.commit()
        principal_id = principal.id
        assert (review.id, review.name, review.status, review.version) == (
            "TYPE-000001",
            "Самосвал",
            "REVIEW",
            1,
        )
        assert (confirmed.id, confirmed.status) == ("TYPE-000002", "CONFIRMED")

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        rolled_back = create_vehicle_type(
            db, "Экскаватор", True, True, True,
            principal=principal, context=save_context(principal)
        )
        assert rolled_back.id == "TYPE-000003"
        db.rollback()

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        after_rollback = create_vehicle_type(
            db, "Грейдер", True, True, True,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert after_rollback.id == "TYPE-000004"

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        with pytest.raises(IntegrityError):
            create_vehicle_type(
                db, "  Самосвал  ", True, True, True,
                principal=principal, context=save_context(principal)
            )
        db.rollback()
        original = db.scalar(select(VehicleType).where(VehicleType.id == "TYPE-000001"))
        assert original is not None
        assert (original.name, original.status, original.version) == ("Самосвал", "REVIEW", 1)

    with Session(engine) as db:
        principal = db.get(User, principal_id)
        after_conflict = create_vehicle_type(
            db, "Трактор", True, False, True,
            principal=principal, context=save_context(principal)
        )
        db.commit()
        assert after_conflict.id == "TYPE-000006"

    def create_parallel(name):
        with Session(engine) as db:
            principal = db.get(User, principal_id)
            vehicle_type = create_vehicle_type(
                db, name, True, False, False,
                principal=principal, context=save_context(principal)
            )
            db.commit()
            return vehicle_type.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        parallel = list(executor.map(create_parallel, ("Каток", "Фреза")))
    assert set(parallel) == {"TYPE-000007", "TYPE-000008"}

    invalid_rows = [
        {"id": "TYPE-900001", "name": "", "requires_sts": True, "status": "REVIEW"},
        {"id": "TYPE-900002", "name": "СТС False", "requires_sts": False, "status": "REVIEW"},
        {"id": "TYPE-900003", "name": "Неверный статус", "requires_sts": True, "status": "INVALID"},
    ]
    for row in invalid_rows:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(VehicleType.__table__.insert().values(**row))

    with engine.connect() as connection:
        vehicle_sequence = connection.exec_driver_sql(
            "SELECT last_value, is_called FROM arm2_vehicle_id_seq"
        ).one()
        type_sequence = connection.exec_driver_sql(
            "SELECT last_value, is_called FROM arm2_vehicle_type_id_seq"
        ).one()
    assert vehicle_sequence == (1, False)
    assert type_sequence == (8, True)
    assert set(VehicleType.__table__.columns.keys()) == {
        "id",
        "name",
        "requires_sts",
        "requires_calibration",
        "requires_wialon",
        "status",
        "version",
    }
    assert [column.name for column in VehicleType.__table__.primary_key] == ["id"]
    engine.dispose()
