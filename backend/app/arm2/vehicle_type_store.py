from sqlalchemy.orm import Session

from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY
from app.arm2.object_save import InvalidExpectedVersionError as InvalidExpectedVersionError
from app.arm2.object_save import (
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_rules import (
    normalize_type_name,
    normalize_type_requirements,
    type_status,
)

VehicleTypeNotFoundError = ObjectNotFoundError
VehicleTypeVersionConflictError = ObjectVersionConflictError


class _VehicleTypeAdapter:
    def normalize(self, values):
        name = normalize_type_name(values["name"])
        sts, calibration, wialon = normalize_type_requirements(
            values["requires_sts"],
            values["requires_calibration"],
            values["requires_wialon"],
        )
        return {
            "name": name,
            "requires_sts": sts,
            "requires_calibration": calibration,
            "requires_wialon": wialon,
            "status": type_status(name, sts, calibration, wialon),
        }

    def create_record(self, db, values):
        vehicle_type = VehicleType(id=next_id(db, "TYPE"), version=1, **values)
        db.add(vehicle_type)
        db.flush()
        return vehicle_type

    def lock_record(self, db, object_id):
        from sqlalchemy import select

        return db.scalar(
            select(VehicleType).where(VehicleType.id == object_id).with_for_update()
        )

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


VEHICLE_TYPE_ADAPTER = _VehicleTypeAdapter()


def register_vehicle_type_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter("TYPE", VEHICLE_TYPE_ADAPTER)


def _values(name, requires_sts, requires_calibration, requires_wialon):
    return {
        "name": name,
        "requires_sts": requires_sts,
        "requires_calibration": requires_calibration,
        "requires_wialon": requires_wialon,
    }


def create_vehicle_type(
    db: Session,
    name: str,
    requires_sts: bool | None = True,
    requires_calibration: bool | None = None,
    requires_wialon: bool | None = None,
    *,
    context: SaveContext,
) -> VehicleType:
    return create_object(
        db,
        object_type="TYPE",
        values=_values(name, requires_sts, requires_calibration, requires_wialon),
        context=context,
    ).record


def update_vehicle_type(
    db: Session,
    type_id: str,
    expected_version: int,
    name: str,
    requires_sts: bool | None,
    requires_calibration: bool | None,
    requires_wialon: bool | None,
    *,
    context: SaveContext,
) -> VehicleType:
    return update_object(
        db,
        object_type="TYPE",
        object_id=type_id,
        expected_version=expected_version,
        values=_values(name, requires_sts, requires_calibration, requires_wialon),
        context=context,
    ).record
