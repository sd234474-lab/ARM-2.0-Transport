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
        with pytest.raises(ValueError, match="only one"):
            update_vehicle(db, vehicle.id, 1, name=vehicle.name, plate_number=vehicle.plate_number,
                           type_id=None, accounting_state=vehicle.accounting_state, model=None, wialon_unit_id=None,
                           owner_org_id=org.id, principal=admin, context=ctx(admin))

        vehicle = update_vehicle(db, vehicle.id, 1, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model="Model 1",
                                 wialon_unit_id=None, principal=admin, context=ctx(admin))
        assert (vehicle.version, vehicle.owner_cp_id, vehicle.owner_org_id) == (2, cp.id, None)
        vehicle = update_vehicle(db, vehicle.id, 2, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=vehicle.model,
                                 wialon_unit_id=None, owner_cp_id=None, owner_org_id=org.id,
                                 principal=admin, context=ctx(admin))
        assert (vehicle.version, vehicle.owner_cp_id, vehicle.owner_org_id) == (3, None, org.id)
        vehicle = update_vehicle(db, vehicle.id, 3, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=vehicle.model,
                                 wialon_unit_id=None,
                                 principal=admin, context=ctx(admin))
        assert vehicle.version == 3
        vehicle = update_vehicle(db, vehicle.id, 3, name="Truck renamed", plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=vehicle.model,
                                 wialon_unit_id=None, principal=admin, context=ctx(admin))
        assert (vehicle.version, vehicle.owner_cp_id, vehicle.owner_org_id) == (4, None, org.id)
        vehicle = update_vehicle(db, vehicle.id, 4, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=vehicle.model,
                                 wialon_unit_id=None, owner_org_id=None,
                                 principal=admin, context=ctx(admin))
        assert (vehicle.version, vehicle.owner_cp_id, vehicle.owner_org_id) == (5, None, None)
        update_counterparty(db, cp.id, 1, cp.name, full_name=None,
                            principal=admin, context=ctx(admin))
        vehicle = update_vehicle(db, vehicle.id, 5, name=vehicle.name, plate_number=vehicle.plate_number,
                                 type_id=None, accounting_state=vehicle.accounting_state, model=vehicle.model,
                                 wialon_unit_id=None, owner_cp_id=cp.id,
                                 principal=admin, context=ctx(admin))
        assert (vehicle.version, vehicle.owner_cp_id, vehicle.owner_org_id) == (6, cp.id, None)
        db.commit()
        events = db.scalars(select(HistoryEvent).where(HistoryEvent.object_id == vehicle_id).order_by(HistoryEvent.event_id)).all()
        assert len(events) == 6
        assert events[0].related_objects[0]["object_id"] == cp.id
        assert events[1].changes["model"] == {"before": None, "after": "Model 1"}
        assert events[2].changes["owner_cp_id"]["after"] is None
        assert events[2].changes["owner_org_id"]["after"] == org.id
        assert events[3].related_objects[0]["object_id"] == org.id
        assert events[4].related_objects == []
        assert events[5].related_objects[0]["object_id"] == cp.id
        assert all(event.actor_id == str(admin.id) for event in events)
    engine.dispose()
