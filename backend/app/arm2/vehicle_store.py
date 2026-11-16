import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2.counterparty_models import Counterparty
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.arm2.organization_models import Organization
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.models import User
from app.permissions import Permission, has_permission

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
            .execution_options(populate_existing=True)
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


_OWNERS = {
    "CP": (Counterparty, Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT),
    "ORG": (Organization, Permission.ARM2_ORG_VIEW, Permission.ARM2_ORG_EDIT),
}


def _owner_pair(kind, identity):
    if kind is None and identity is None:
        return None, None
    if kind not in _OWNERS or not isinstance(identity, str):
        raise ValueError("Владелец должен быть существующим CP/ORG либо пустым")
    OBJECT_REGISTRY.require(kind).validate_id(identity)
    return kind, identity


def _link_user(db, principal, permissions):
    if not isinstance(principal, User) or type(principal.id) is not int:
        raise ObjectAuthorizationError("Требуется действующий пользователь")
    with db.no_autoflush:
        user = db.get(User, principal.id, populate_existing=True)
    if user is None or not user.active or user.deleted_at is not None or any(
        not has_permission(user, permission) for permission in permissions
    ):
        raise ObjectAuthorizationError("Нет прав на изменение операционного владельца")
    return user


def set_operational_owner(db, vehicle_id, expected_version, target_kind, target_id,
                          expected_owner_kind, expected_owner_id, confirm_reassignment,
                          *, principal, context, initiator_kind=None, initiator_id=None):
    target = _owner_pair(target_kind, target_id)
    expected = _owner_pair(expected_owner_kind, expected_owner_id)
    initiator = _owner_pair(initiator_kind, initiator_id)
    if type(expected_version) is not int or expected_version <= 0 or type(confirm_reassignment) is not bool:
        raise ValueError("Некорректная версия или подтверждение операции")
    needed = [Permission.ARM2_VEH_VIEW, Permission.ARM2_VEH_EDIT]
    _link_user(db, principal, needed)
    OBJECT_REGISTRY.require("VEH").validate_id(vehicle_id)
    record = VEHICLE_ADAPTER.lock_record(db, vehicle_id)
    if record is None:
        raise ObjectNotFoundError("Техника не найдена")
    actual = ("CP", record.owner_cp_id) if record.owner_cp_id else (
        ("ORG", record.owner_org_id) if record.owner_org_id else (None, None)
    )
    # Permission checks use the actual owner under the VEH lock, never the UI claim.
    affected = sorted({pair for pair in (actual, target, initiator) if pair[0]})
    for kind, _identity in affected:
        _model, view, edit = _OWNERS[kind]
        needed.extend((view, edit))
    user = _link_user(db, principal, needed)
    if record.version != expected_version or actual != expected:
        raise ObjectVersionConflictError("Версия или операционный владелец изменены; сравните с сервером")
    if initiator[0] and (target[0] or actual[0]) and initiator != (target if target[0] else actual):
        raise ValueError("Карточка-инициатор не соответствует прежнему или новому владельцу")
    for kind, identity in affected:
        model = _OWNERS[kind][0]
        if db.scalar(select(model.id).where(model.id == identity).with_for_update(read=True, key_share=True)) is None:
            raise ObjectNotFoundError("Владелец не найден")
    if actual[0] and actual != target and not confirm_reassignment:
        raise ObjectVersionConflictError("Требуется явное подтверждение изменения прежнего владельца")
    untouched = {field: getattr(record, field) for field in
                 ("name", "plate_number", "type_id", "accounting_state", "model", "wialon_unit_id")}
    with db.begin_nested():
        saved = update_object(db, object_type="VEH", object_id=vehicle_id, expected_version=expected_version,
                              values={"owner_cp_id": target_id if target_kind == "CP" else None,
                                      "owner_org_id": target_id if target_kind == "ORG" else None},
                              partial=True, principal=user, context=context)
        if any(getattr(saved.record, field) != value for field, value in untouched.items()):
            raise ValueError("Связь не сохранена: нормализация затронула бы другие поля. Сначала явно сохраните основную карточку VEH.")
    return saved
