from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import ObjectVersionConflictError, SaveContext
from app.arm2.routes import router
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle, update_vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type
from app.auth import current_user
from app.db import get_db
from app.models import User


@pytest.fixture
def type_db(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url)
    with Session(engine, expire_on_commit=False) as db:
        user = User(login="type-b4", name="TYPE", role="ADMIN_OPERATOR", password_hash="unused", active=True)
        db.add(user)
        db.commit()
        yield db, user, SaveContext("HUMAN", str(user.id), "type-test")
    engine.dispose()


def test_type_completeness_sts_and_vehicle_compatibility(type_db):
    db, user, ctx = type_db
    args = dict(principal=user, context=ctx)
    incomplete = create_vehicle_type(db, "Без категории", True, False, False, **args)
    car = create_vehicle_type(db, "Самосвал", True, False, False, plate_category="Автомобильный", **args)
    assert incomplete.status == "REVIEW" and car.status == "CONFIRMED"
    vehicle = create_vehicle(db, "Машина", "А123ВС86", type_id=car.id, **args)
    db.commit()
    for sts in (None, False):
        with pytest.raises(ValueError, match="СТС"):
            create_vehicle_type(db, "Неверный СТС", sts, **args)
        with pytest.raises(ValueError, match="СТС"):
            update_vehicle_type(db, car.id, car.version, car.name, sts, False, False,
                                plate_category="Автомобильный", **args)
    with pytest.raises(ValueError, match="связанной техникой"):
        update_vehicle_type(db, car.id, car.version, car.name, True, False, False,
                            plate_category="Самоходная техника", **args)
    with pytest.raises(ValueError, match="несовместим"):
        create_vehicle(db, "Трактор", "1234АА86", type_id=car.id, **args)
    with pytest.raises(ValueError):
        create_vehicle(db, "Нельзя", "А124ВС86", type_id=incomplete.id, **args)
    old_version = car.version
    updated = update_vehicle_type(db, car.id, old_version, "Новое имя", True, False, False,
                                 plate_category="Автомобильный", **args)
    db.commit()
    assert updated.version == old_version + 1 and db.get(Vehicle, vehicle.id).type_id == car.id
    with pytest.raises(ObjectVersionConflictError):
        update_vehicle_type(db, car.id, old_version, "Старое", True, False, False,
                            plate_category="Автомобильный", **args)
    db.expire_all()
    assert db.get(VehicleType, car.id).plate_category == "Автомобильный"
    assert len(db.scalars(select(HistoryEvent).where(HistoryEvent.object_id == car.id)).all()) == 2


def test_historical_review_link_survives_and_can_be_repaired(type_db):
    db, user, ctx = type_db
    # Simulate preserved historical NULL: commands must not silently repair it.
    old = VehicleType(id="TYPE-900001", name="Исторический", requires_sts=None,
                      requires_calibration=False, requires_wialon=False, plate_category=None, status="REVIEW", version=1)
    db.add(old)
    db.flush()
    db.execute(text("UPDATE arm2_vehicle_types SET requires_sts=NULL WHERE id='TYPE-900001'"))
    db.refresh(old)
    vehicle = Vehicle(id="VEH-900001", name="Историческая машина", plate_number="А125ВС86", type_id=old.id,
                      accounting_state="Действующая", version=1)
    db.add(vehicle)
    db.commit()
    update_vehicle(db, vehicle.id, 1, name="Новое имя", plate_number=vehicle.plate_number,
                   type_id=old.id, accounting_state="Действующая", model=None, wialon_unit_id=None,
                   principal=user, context=ctx)
    assert old.requires_sts is None
    with pytest.raises(ValueError, match="связанной техникой"):
        update_vehicle_type(db, old.id, 1, old.name, True, False, False,
                            plate_category="Самоходная техника", principal=user, context=ctx)
    repaired = update_vehicle_type(db, old.id, 1, old.name, True, False, False,
                                   plate_category="Автомобильный", principal=user, context=ctx)
    db.commit()
    assert repaired.status == "CONFIRMED" and vehicle.type_id == old.id


def test_type_search_server_pagination_status_and_permissions(type_db):
    db, user, _ = type_db
    db.add_all([VehicleType(id=f"TYPE-{i+100000:06}", name=f"Тип {i:03}", requires_sts=True,
                           requires_calibration=False, requires_wialon=False,
                           plate_category="Автомобильный" if i == 104 else None,
                           status="CONFIRMED" if i == 104 else "REVIEW", version=1) for i in range(105)])
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        for value in (None, False, 1, "true"):
            assert client.post("/api/arm2/types", json={"name": "Недопустимый СТС", "requires_sts": value}).status_code == 422
        result = client.get("/api/arm2/types/search?status=CONFIRMED").json()
        assert result["matched"] == 1 and result["items"][0]["name"] == "Тип 104"
        assert client.get("/api/arm2/types/search?q=104").json()["matched"] == 1
        assert len(client.get("/api/arm2/types/search?offset=100").json()["items"]) == 5
        assert client.get("/api/arm2/types/search?status=READY").status_code == 422
        user.role, user.permissions = "OPERATOR", []
        db.commit()
        assert client.get("/api/arm2/types/search").status_code == 403


def test_type_category_migration_preserves_existing_links(isolated_arm2_database_url, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from app.config import settings
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "0064")
    engine = create_engine(isolated_arm2_database_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO arm2_vehicle_types(id,name,requires_sts,requires_calibration,requires_wialon,status,version) VALUES ('TYPE-700001','Старый',NULL,false,false,'CONFIRMED',4)"))
        conn.execute(text("INSERT INTO arm2_vehicles(id,name,plate_number,type_id,accounting_state,version) VALUES ('VEH-700001','Старая','А111АА86','TYPE-700001','Действующая',7)"))
    command.upgrade(config, "head")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT plate_category,status,version,requires_sts FROM arm2_vehicle_types")).one()
        assert tuple(row) == (None, "REVIEW", 5, None)
        assert conn.execute(text("SELECT type_id,version FROM arm2_vehicles")).one() == ("TYPE-700001", 7)
        assert conn.scalar(text("SELECT count(*) FROM arm2_history_events WHERE correlation_id='migration-0067'")) == 1
    engine.dispose()


def test_category_change_serializes_with_vehicle_assignment(type_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    db, user, ctx = type_db
    car = create_vehicle_type(db, "Конкурентный", True, False, False,
                              plate_category="Автомобильный", principal=user, context=ctx)
    db.commit()
    type_id, user_id = car.id, user.id
    ready, release = Event(), Event()
    engine = db.get_bind()

    def assign():
        with Session(engine) as other:
            actor = other.get(User, user_id)
            create_vehicle(other, "Конкурентная машина", "А777АА86", type_id=type_id,
                           principal=actor, context=ctx)
            ready.set()
            assert release.wait(10)
            other.commit()

    def change_category():
        with Session(engine) as other:
            actor = other.get(User, user_id)
            with pytest.raises(ValueError, match="связанной техникой"):
                update_vehicle_type(other, type_id, 1, "Конкурентный", True, False, False,
                                    plate_category="Самоходная техника", principal=actor, context=ctx)
            other.rollback()

    with ThreadPoolExecutor(2) as pool:
        assignment = pool.submit(assign)
        assert ready.wait(10)
        change = pool.submit(change_category)
        release.set()
        assignment.result(15)
        change.result(15)
    db.expire_all()
    assert db.get(VehicleType, type_id).plate_category == "Автомобильный"
    assert db.scalar(select(Vehicle.id).where(Vehicle.type_id == type_id)) is not None
