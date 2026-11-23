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
    normalize_plate_category,
    normalize_type_name,
    normalize_type_requirements,
    plate_compatible,
    type_status,
)
from app.models import User

VehicleTypeNotFoundError = ObjectNotFoundError
VehicleTypeVersionConflictError = ObjectVersionConflictError
_UNSET = object()


class _VehicleTypeAdapter:
    def normalize(self, values):
        name = normalize_type_name(values["name"])
        sts, calibration, wialon = normalize_type_requirements(
            values["requires_sts"],
            values["requires_calibration"],
            values["requires_wialon"],
        )
        return {
            "plate_category": normalize_plate_category(values["plate_category"]),
            "name": name,
            "requires_sts": sts,
            "requires_calibration": calibration,
            "requires_wialon": wialon,
        }

    def create_record(self, db, values):
        vehicle_type = VehicleType(id=next_id(db, "TYPE"), version=1, **values)
        db.add(vehicle_type)
        db.flush()
        return vehicle_type

    def lock_record(self, db, object_id):
        from sqlalchemy import select

        return db.scalar(
            select(VehicleType).where(VehicleType.id == object_id).with_for_update().execution_options(populate_existing=True)
        )

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


VEHICLE_TYPE_ADAPTER = _VehicleTypeAdapter()


def _vehicle_type_status(values):
    return type_status(
        values["name"],
        values["requires_sts"],
        values["requires_calibration"],
        values["requires_wialon"],
        values["plate_category"],
    )


def _guard_category(db, current, values):
    if current is not None and current.plate_category != values["plate_category"] and values["plate_category"] is not None:
        from sqlalchemy import select

        from app.arm2.vehicle_models import Vehicle
        plates = db.scalars(select(Vehicle.plate_number).where(Vehicle.type_id == current.id))
        if any(not plate_compatible(p, values["plate_category"]) for p in plates):
            raise ValueError("Тип государственного номера несовместим со связанной техникой. Сначала исправьте связи или номера машин")
    return ()


def register_vehicle_type_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter(
        "TYPE",
        VEHICLE_TYPE_ADAPTER,
        status_field="status",
        status_rule=_vehicle_type_status,
        reference_rules=(_guard_category,),
    )


def _values(name, requires_sts, requires_calibration, requires_wialon, plate_category):
    return {
        "name": name,
        "plate_category": plate_category,
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
    plate_category: str | None = None,
    principal: User,
    context: SaveContext,
) -> VehicleType:
    return create_object(
        db,
        object_type="TYPE",
        values=_values(name, requires_sts, requires_calibration, requires_wialon, plate_category),
        principal=principal,
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
    plate_category=_UNSET,
    principal: User,
    context: SaveContext,
) -> VehicleType:
    values = _values(name, requires_sts, requires_calibration, requires_wialon, plate_category)
    if plate_category is _UNSET:
        values.pop("plate_category")
    return update_object(
        db,
        object_type="TYPE",
        object_id=type_id,
        expected_version=expected_version,
        values=values,
        partial=True,
        principal=principal,
        context=context,
    ).record
