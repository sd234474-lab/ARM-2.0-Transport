from sqlalchemy.orm import Session

from app.arm2.ids import next_id
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_rules import (
    normalize_type_name,
    normalize_type_requirements,
    type_status,
)


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
