import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2.counterparty_models import Counterparty
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import SaveContext, create_object, update_object
from app.arm2.organization_models import Organization
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.models import User

_ALLOWED_LETTERS = "АВЕКМНОРСТУХ"
_LATIN_TO_CYRILLIC = str.maketrans(
    {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
     "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х"}
)
_CAR = re.compile(rf"[{_ALLOWED_LETTERS}][0-9]{{3}}[{_ALLOWED_LETTERS}]{{2}}[0-9]{{2,3}}\Z")
_SELF_PROPELLED = re.compile(rf"[0-9]{{4}}[{_ALLOWED_LETTERS}]{{2}}[0-9]{{2}}\Z")


class _Unset:
    pass


_UNSET = _Unset()


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
            "owner_cp_id": values["owner_cp_id"],
            "owner_org_id": values["owner_org_id"],
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


def _vehicle_owner_reference(db, current, values):
    cp_id, org_id = values["owner_cp_id"], values["owner_org_id"]
    if cp_id is not None and org_id is not None:
        raise ValueError("VEH can have only one operational owner")
    if cp_id is not None:
        if db.get(Counterparty, cp_id) is None:
            raise ValueError("Counterparty owner does not exist")
        return (ObjectReference("CP", cp_id, "operational_owner"),)
    if org_id is not None:
        if db.get(Organization, org_id) is None:
            raise ValueError("Organization owner does not exist")
        return (ObjectReference("ORG", org_id, "operational_owner"),)
    return ()


def register_vehicle_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter(
        "VEH",
        VEHICLE_ADAPTER,
        reference_rules=(_vehicle_type_reference, _vehicle_owner_reference),
    )


def _values(name, plate_number, type_id, accounting_state, model, wialon_unit_id, owner_cp_id, owner_org_id):
    values = {
        "name": name,
        "plate_number": plate_number,
        "type_id": type_id,
        "accounting_state": accounting_state,
        "model": model,
        "wialon_unit_id": wialon_unit_id,
    }
    if owner_cp_id is not _UNSET:
        values["owner_cp_id"] = owner_cp_id
    if owner_org_id is not _UNSET:
        values["owner_org_id"] = owner_org_id
    return values


def create_vehicle(
    db: Session,
    name: str,
    plate_number: str,
    *,
    type_id: str | None = None,
    accounting_state: str = "Действующая",
    model: str | None = None,
    wialon_unit_id: str | None = None,
    owner_cp_id: str | None = None,
    owner_org_id: str | None = None,
    principal: User,
    context: SaveContext,
) -> Vehicle:
    return create_object(
        db,
        object_type="VEH",
        values=_values(name, plate_number, type_id, accounting_state, model, wialon_unit_id, owner_cp_id, owner_org_id),
        principal=principal,
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
    owner_cp_id: str | None | _Unset = _UNSET,
    owner_org_id: str | None | _Unset = _UNSET,
    principal: User,
    context: SaveContext,
) -> Vehicle:
    return update_object(
        db,
        object_type="VEH",
        object_id=vehicle_id,
        expected_version=expected_version,
        values=_values(name, plate_number, type_id, accounting_state, model, wialon_unit_id, owner_cp_id, owner_org_id),
        partial=True,
        principal=principal,
        context=context,
    ).record
