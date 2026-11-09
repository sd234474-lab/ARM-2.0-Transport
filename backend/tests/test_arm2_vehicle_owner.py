import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext
from app.arm2.organization_store import create_organization
from app.arm2.vehicle_store import create_vehicle, update_vehicle
from app.models import User


def ctx(user): return SaveContext("HUMAN", str(user.id), "owner-test")


def test_vehicle_operational_owner_cp_org_null_and_history(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        admin = User(login="owner-admin", name="owner-admin", password_hash="unused", role="ADMIN_OPERATOR", active=True, permissions=[])
        db.add(admin)
        db.flush()
        cp = create_counterparty(db, "CP", full_name="CP full", legal_address="Address", principal=admin, context=ctx(admin))
        org = create_organization(db, "ORG", principal=admin, context=ctx(admin))
        vehicle = create_vehicle(db, "Truck", "A123BC86", owner_cp_id=cp.id, principal=admin, context=ctx(admin))
        vehicle_id = vehicle.id
        assert vehicle.owner_cp_id == cp.id and vehicle.owner_org_id is None
        with pytest.raises(ValueError, match="only one"):
            update_vehicle(db, vehicle.id, 1, name=vehicle.name, plate_number=vehicle.plate_number,
                           type_id=None, accounting_state=vehicle.accounting_state, model=None, wialon_unit_id=None,
                           owner_cp_id=cp.id, owner_org_id=org.id, principal=admin, context=ctx(admin))
        vehicle = update_vehicle(db, vehicle.id, 1, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=None, wialon_unit_id=None,
                                 owner_org_id=org.id, principal=admin, context=ctx(admin))
        assert vehicle.id == vehicle_id and vehicle.owner_org_id == org.id
        update_counterparty(db, cp.id, 1, cp.name, full_name=None, legal_address="Address", principal=admin, context=ctx(admin))
        vehicle = update_vehicle(db, vehicle.id, 2, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=None, wialon_unit_id=None,
                                 principal=admin, context=ctx(admin))
        assert vehicle.owner_cp_id is None and vehicle.owner_org_id is None
        db.commit()
        events = db.scalars(select(HistoryEvent).where(HistoryEvent.object_id == vehicle_id).order_by(HistoryEvent.event_id)).all()
        assert len(events) == 3 and events[0].related_objects[0]["object_id"] == cp.id
    engine.dispose()
