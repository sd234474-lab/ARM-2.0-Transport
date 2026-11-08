from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2.ids import next_id
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_rules import (
    normalize_type_name,
    normalize_type_requirements,
    type_status,
)


class InvalidExpectedVersionError(ValueError):
    pass


class VehicleTypeNotFoundError(LookupError):
    pass


class VehicleTypeVersionConflictError(RuntimeError):
    pass


def create_vehicle_type(
    db: Session,
    name: str,
    requires_sts: bool | None = True,
    requires_calibration: bool | None = None,
    requires_wialon: bool | None = None,
) -> VehicleType:
    normalized_name = normalize_type_name(name)
    sts, calibration, wialon = normalize_type_requirements(
        requires_sts,
        requires_calibration,
        requires_wialon,
    )
    vehicle_type = VehicleType(
        id=next_id(db, "TYPE"),
        name=normalized_name,
        requires_sts=sts,
        requires_calibration=calibration,
        requires_wialon=wialon,
        status=type_status(normalized_name, sts, calibration, wialon),
        version=1,
    )
    db.add(vehicle_type)
    db.flush()
    return vehicle_type


def update_vehicle_type(
    db: Session,
    type_id: str,
    expected_version: int,
    name: str,
    requires_sts: bool | None,
    requires_calibration: bool | None,
    requires_wialon: bool | None,
) -> VehicleType:
    if type(expected_version) is not int or expected_version <= 0:
        raise InvalidExpectedVersionError("expected_version must be a positive integer")

    vehicle_type = db.scalar(
        select(VehicleType).where(VehicleType.id == type_id).with_for_update()
    )
    if vehicle_type is None:
        raise VehicleTypeNotFoundError(f"Vehicle type not found: {type_id}")
    if vehicle_type.version != expected_version:
        raise VehicleTypeVersionConflictError(
            f"Vehicle type version conflict: expected {expected_version}, current {vehicle_type.version}"
        )

    normalized_name = normalize_type_name(name)
    sts, calibration, wialon = normalize_type_requirements(
        requires_sts,
        requires_calibration,
        requires_wialon,
    )
    status = type_status(normalized_name, sts, calibration, wialon)
    values = (normalized_name, sts, calibration, wialon, status)
    current = (
        vehicle_type.name,
        vehicle_type.requires_sts,
        vehicle_type.requires_calibration,
        vehicle_type.requires_wialon,
        vehicle_type.status,
    )
    if values == current:
        return vehicle_type

    (
        vehicle_type.name,
        vehicle_type.requires_sts,
        vehicle_type.requires_calibration,
        vehicle_type.requires_wialon,
        vehicle_type.status,
    ) = values
    vehicle_type.version += 1
    db.flush()
    return vehicle_type
