from dataclasses import dataclass, replace
from typing import Any, Protocol

from sqlalchemy.orm import Session


class ObjectRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    types: tuple[type, ...]
    nullable: bool = False

    def validate(self, value: Any, field: str) -> None:
        if value is None:
            if self.nullable:
                return
            raise ObjectRegistryError(f"{field} must not be None")
        if type(value) not in self.types:
            names = ", ".join(item.__name__ for item in self.types)
            raise ObjectRegistryError(f"{field} must have type {names}")


class ObjectAdapter(Protocol):
    def normalize(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def create_record(self, db: Session, values: dict[str, Any]) -> Any: ...

    def lock_record(self, db: Session, object_id: str) -> Any | None: ...

    def apply(self, record: Any, values: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class ObjectDefinition:
    object_type: str
    id_prefix: str
    command_fields: dict[str, FieldSpec]
    history_fields: dict[str, FieldSpec]
    operations: frozenset[str] = frozenset({"CREATED", "UPDATED"})
    adapter: ObjectAdapter | None = None

    def validate_command(self, values: dict[str, Any]) -> None:
        if type(values) is not dict:
            raise ObjectRegistryError("Object command must be a plain object")
        supplied = set(values)
        expected = set(self.command_fields)
        if supplied != expected:
            unknown = supplied - expected
            missing = expected - supplied
            details = []
            if unknown:
                details.append(f"unknown: {', '.join(sorted(unknown))}")
            if missing:
                details.append(f"missing: {', '.join(sorted(missing))}")
            raise ObjectRegistryError("Invalid command fields (" + "; ".join(details) + ")")
        for field, spec in self.command_fields.items():
            spec.validate(values[field], field)

    def snapshot(self, record: Any) -> dict[str, Any]:
        snapshot = {}
        for field, spec in self.history_fields.items():
            value = getattr(record, field)
            spec.validate(value, field)
            snapshot[field] = value
        return snapshot

    def validate_id(self, object_id: str) -> None:
        prefix = self.id_prefix + "-"
        number = object_id[len(prefix) :] if isinstance(object_id, str) and object_id.startswith(prefix) else ""
        if len(number) < 6 or not number.isdigit():
            raise ObjectRegistryError(f"Invalid {self.object_type} object_id")


class ObjectRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ObjectDefinition] = {}

    def register(self, definition: ObjectDefinition) -> None:
        if definition.object_type in self._definitions:
            raise ObjectRegistryError(f"Object type already registered: {definition.object_type}")
        self._definitions[definition.object_type] = definition

    def attach_adapter(self, object_type: str, adapter: ObjectAdapter) -> None:
        definition = self.require(object_type)
        if definition.adapter is adapter:
            return
        if definition.adapter is not None:
            raise ObjectRegistryError(f"Object type already has an adapter: {object_type}")
        self._definitions[object_type] = replace(definition, adapter=adapter)

    def require(self, object_type: str) -> ObjectDefinition:
        try:
            return self._definitions[object_type]
        except KeyError as error:
            raise ObjectRegistryError(f"Unknown object_type: {object_type}") from error


TYPE_COMMAND_FIELDS = {
    "name": FieldSpec((str,)),
    "requires_sts": FieldSpec((bool,), nullable=True),
    "requires_calibration": FieldSpec((bool,), nullable=True),
    "requires_wialon": FieldSpec((bool,), nullable=True),
}
TYPE_HISTORY_FIELDS = {
    **TYPE_COMMAND_FIELDS,
    "status": FieldSpec((str,)),
    "version": FieldSpec((int,)),
}

OBJECT_REGISTRY = ObjectRegistry()
OBJECT_REGISTRY.register(
    ObjectDefinition("TYPE", "TYPE", TYPE_COMMAND_FIELDS, TYPE_HISTORY_FIELDS)
)
VEH_COMMAND_FIELDS = {
    "name": FieldSpec((str,)),
    "plate_number": FieldSpec((str,)),
    "type_id": FieldSpec((str,), nullable=True),
    "accounting_state": FieldSpec((str,)),
    "model": FieldSpec((str,), nullable=True),
    "wialon_unit_id": FieldSpec((str,), nullable=True),
}
VEH_HISTORY_FIELDS = {
    **VEH_COMMAND_FIELDS,
    "version": FieldSpec((int,)),
}
OBJECT_REGISTRY.register(
    ObjectDefinition("VEH", "VEH", VEH_COMMAND_FIELDS, VEH_HISTORY_FIELDS)
)


def initialize_object_adapters() -> ObjectRegistry:
    """Idempotently attach every production adapter to the shared registry."""
    from app.arm2.vehicle_type_store import register_vehicle_type_adapter

    register_vehicle_type_adapter(OBJECT_REGISTRY)
    from app.arm2.vehicle_store import register_vehicle_adapter

    register_vehicle_adapter(OBJECT_REGISTRY)
    return OBJECT_REGISTRY
