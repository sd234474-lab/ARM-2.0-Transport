import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import SaveContext, create_object, update_object
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType

_ALLOWED_LETTERS = "АВЕКМНОРСТУХ"
_LATIN_TO_CYRILLIC = str.maketrans(
    {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
     "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х"}
)
_CAR = re.compile(rf"[{_ALLOWED_LETTERS}][0-9]{{3}}[{_ALLOWED_LETTERS}]{{2}}[0-9]{{2,3}}\Z")
_SELF_PROPELLED = re.compile(rf"[0-9]{{4}}[{_ALLOWED_LETTERS}]{{2}}[0-9]{{2}}\Z")


def normalize_plate_number(value: str) -> str:
    """Only approved forms; questionnaire separator policy is outside this slice."""
    if not isinstance(value, str):
        raise ValueError("plate_number must be text")
    plate = value.strip().upper().translate(_LATIN_TO_CYRILLIC)
    if not (_CAR.fullmatch(plate) or _SELF_PROPELLED.fullmatch(plate)):
        raise ValueError("Invalid plate_number format")
    return plate


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class _VehicleAdapter:
    def normalize(self, values):
        name = values["name"].strip()
        if not name:
            raise ValueError("Vehicle name must not be empty")
        state = values["accounting_state"]
        if state not in {"Действующая", "Архивная"}:
            raise ValueError("Invalid vehicle accounting_state")
        return {
            "name": name,
            "plate_number": normalize_plate_number(values["plate_number"]),
            "type_id": values["type_id"],
            "accounting_state": state,
            "model": _optional_text(values["model"]),
            "wialon_unit_id": _optional_text(values["wialon_unit_id"]),
        }

    def create_record(self, db, values):
        record = Vehicle(id=next_id(db, "VEH"), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        return db.scalar(
            select(Vehicle).where(Vehicle.id == object_id).with_for_update()
        )

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


VEHICLE_ADAPTER = _VehicleAdapter()


def _vehicle_type_reference(db, current, values):
    type_id = values["type_id"]
    if type_id is None:
        return ()
    # An existing link remains valid if TYPE later becomes incomplete. Only a
    # newly assigned or changed link must satisfy the current subject rule.
    if current is None or current.type_id != type_id:
        vehicle_type = db.scalar(select(VehicleType).where(VehicleType.id == type_id))
        if vehicle_type is None or vehicle_type.status != "CONFIRMED":
            raise ValueError("Only an existing CONFIRMED TYPE can be assigned to VEH")
    return (ObjectReference("TYPE", type_id, "vehicle_type"),)


def register_vehicle_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter(
        "VEH",
        VEHICLE_ADAPTER,
        reference_rules=(_vehicle_type_reference,),
    )


def _values(name, plate_number, type_id, accounting_state, model, wialon_unit_id):
    return {
        "name": name,
        "plate_number": plate_number,
        "type_id": type_id,
        "accounting_state": accounting_state,
        "model": model,
        "wialon_unit_id": wialon_unit_id,
    }


def create_vehicle(
    db: Session,
    name: str,
    plate_number: str,
    *,
    type_id: str | None = None,
    accounting_state: str = "Действующая",
    model: str | None = None,
    wialon_unit_id: str | None = None,
    context: SaveContext,
) -> Vehicle:
    return create_object(
        db,
        object_type="VEH",
        values=_values(name, plate_number, type_id, accounting_state, model, wialon_unit_id),
        context=context,
    ).record


def update_vehicle(
    db: Session,
    vehicle_id: str,
    expected_version: int,
    *,
    name: str,
    plate_number: str,
    type_id: str | None,
    accounting_state: str,
    model: str | None,
    wialon_unit_id: str | None,
    context: SaveContext,
) -> Vehicle:
    return update_object(
        db,
        object_type="VEH",
        object_id=vehicle_id,
        expected_version=expected_version,
        values=_values(name, plate_number, type_id, accounting_state, model, wialon_unit_id),
        context=context,
    ).record
