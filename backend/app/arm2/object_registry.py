from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from app.permissions import Permission


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


StatusRule = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ObjectReference:
    object_type: str
    object_id: str
    relation: str


ReferenceRule = Callable[
    [Session, Any | None, dict[str, Any]], tuple[ObjectReference, ...]
]


@dataclass(frozen=True)
class ObjectDefinition:
    object_type: str
    id_prefix: str
    command_fields: dict[str, FieldSpec]
    history_fields: dict[str, FieldSpec]
    operations: frozenset[str] = frozenset({"CREATED", "UPDATED"})
    adapter: ObjectAdapter | None = None
    status_field: str | None = None
    status_rule: StatusRule | None = None
    reference_rules: tuple[ReferenceRule, ...] = ()
    edit_permission: Permission | None = None

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

    def apply_status(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.status_field is None and self.status_rule is None:
            return values
        if self.status_field is None or self.status_rule is None:
            raise ObjectRegistryError(
                f"Incomplete status configuration for {self.object_type}"
            )
        if self.status_field in values:
            raise ObjectRegistryError(
                f"Adapter must not set platform status field: {self.status_field}"
            )
        try:
            spec = self.history_fields[self.status_field]
        except KeyError as error:
            raise ObjectRegistryError(
                f"Status field is not registered for history: {self.status_field}"
            ) from error
        result = dict(values)
        status = self.status_rule(result)
        spec.validate(status, self.status_field)
        result[self.status_field] = status
        return result


class ObjectRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ObjectDefinition] = {}

    def register(self, definition: ObjectDefinition) -> None:
        if definition.object_type in self._definitions:
            raise ObjectRegistryError(f"Object type already registered: {definition.object_type}")
        self._definitions[definition.object_type] = definition

    def attach_adapter(
        self,
        object_type: str,
        adapter: ObjectAdapter,
        *,
        status_field: str | None = None,
        status_rule: StatusRule | None = None,
        reference_rules: tuple[ReferenceRule, ...] = (),
    ) -> None:
        definition = self.require(object_type)
        if definition.adapter is adapter:
            if (
                definition.status_field == status_field
                and definition.status_rule is status_rule
                and definition.reference_rules == reference_rules
            ):
                return
            raise ObjectRegistryError(
                f"Object type already has different platform rules: {object_type}"
            )
        if definition.adapter is not None:
            raise ObjectRegistryError(f"Object type already has an adapter: {object_type}")
        if (status_field is None) != (status_rule is None):
            raise ObjectRegistryError(
                f"Status field and rule must be configured together: {object_type}"
            )
        self._definitions[object_type] = replace(
            definition,
            adapter=adapter,
            status_field=status_field,
            status_rule=status_rule,
            reference_rules=reference_rules,
        )

    def require(self, object_type: str) -> ObjectDefinition:
        try:
            return self._definitions[object_type]
        except KeyError as error:
            raise ObjectRegistryError(f"Unknown object_type: {object_type}") from error

    def validate_references(
        self,
        definition: ObjectDefinition,
        db: Session,
        current: Any | None,
        values: dict[str, Any],
    ) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for rule in definition.reference_rules:
            for reference in rule(db, current, values):
                if not isinstance(reference, ObjectReference):
                    raise ObjectRegistryError(
                        f"Invalid reference rule result for {definition.object_type}"
                    )
                target = self.require(reference.object_type)
                target.validate_id(reference.object_id)
                relation = reference.relation.strip()
                if not relation:
                    raise ObjectRegistryError("Reference relation must not be empty")
                key = (reference.object_type, reference.object_id, relation)
                if key in seen:
                    raise ObjectRegistryError("Duplicate object reference")
                seen.add(key)
                references.append(
                    {
                        "object_type": reference.object_type,
                        "object_id": reference.object_id,
                        "relation": relation,
                    }
                )
        return references


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
    ObjectDefinition(
        "TYPE",
        "TYPE",
        TYPE_COMMAND_FIELDS,
        TYPE_HISTORY_FIELDS,
        edit_permission=Permission.ARM2_TYPE_EDIT,
    )
)

PARTY_COMMAND_FIELDS = {
    "name": FieldSpec((str,)), "full_name": FieldSpec((str,), nullable=True),
    "legal_address": FieldSpec((str,), nullable=True), "postal_address": FieldSpec((str,), nullable=True),
    "legal_form": FieldSpec((str,), nullable=True), "short_name": FieldSpec((str,), nullable=True),
    "inn": FieldSpec((str,), nullable=True), "kpp": FieldSpec((str,), nullable=True),
    "ogrn": FieldSpec((str,), nullable=True), "phone": FieldSpec((str,), nullable=True),
    "email": FieldSpec((str,), nullable=True), "bank_name": FieldSpec((str,), nullable=True),
    "bik": FieldSpec((str,), nullable=True), "settlement_account": FieldSpec((str,), nullable=True),
    "correspondent_account": FieldSpec((str,), nullable=True),
    "primary_signer_id": FieldSpec((str,), nullable=True),
}
PARTY_HISTORY_FIELDS = {**PARTY_COMMAND_FIELDS, "status": FieldSpec((str,)), "version": FieldSpec((int,))}
OBJECT_REGISTRY.register(ObjectDefinition("CP", "CP", PARTY_COMMAND_FIELDS, PARTY_HISTORY_FIELDS,
                                          edit_permission=Permission.ARM2_CP_EDIT))
OBJECT_REGISTRY.register(ObjectDefinition("ORG", "ORG", PARTY_COMMAND_FIELDS, PARTY_HISTORY_FIELDS,
                                          edit_permission=Permission.ARM2_ORG_EDIT))

REP_COMMAND_FIELDS = {
    "parent_id": FieldSpec((str,)), "name": FieldSpec((str,)),
    "position": FieldSpec((str,), nullable=True), "authority_basis": FieldSpec((str,), nullable=True),
    "phone": FieldSpec((str,), nullable=True), "email": FieldSpec((str,), nullable=True),
    "state": FieldSpec((str,)), "user_id": FieldSpec((int,), nullable=True),
    "last_name": FieldSpec((str,), nullable=True), "first_name": FieldSpec((str,), nullable=True),
    "middle_name": FieldSpec((str,), nullable=True),
}
REP_HISTORY_FIELDS = {**REP_COMMAND_FIELDS, "version": FieldSpec((int,))}
OBJECT_REGISTRY.register(ObjectDefinition("CPREP", "CPREP", REP_COMMAND_FIELDS, REP_HISTORY_FIELDS,
                                          edit_permission=Permission.ARM2_CP_EDIT))
OBJECT_REGISTRY.register(ObjectDefinition("ORGREP", "ORGREP", REP_COMMAND_FIELDS, REP_HISTORY_FIELDS,
                                          edit_permission=Permission.ARM2_ORG_EDIT))
VEH_COMMAND_FIELDS = {
    "name": FieldSpec((str,)),
    "plate_number": FieldSpec((str,)),
    "type_id": FieldSpec((str,), nullable=True),
    "accounting_state": FieldSpec((str,)),
    "model": FieldSpec((str,), nullable=True),
    "wialon_unit_id": FieldSpec((str,), nullable=True),
    "owner_cp_id": FieldSpec((str,), nullable=True),
    "owner_org_id": FieldSpec((str,), nullable=True),
}
VEH_HISTORY_FIELDS = {
    **VEH_COMMAND_FIELDS,
    "version": FieldSpec((int,)),
}
OBJECT_REGISTRY.register(
    ObjectDefinition(
        "VEH",
        "VEH",
        VEH_COMMAND_FIELDS,
        VEH_HISTORY_FIELDS,
        edit_permission=Permission.ARM2_VEH_EDIT,
    )
)


def initialize_object_adapters() -> ObjectRegistry:
    """Idempotently attach every production adapter to the shared registry."""
    from app.arm2.vehicle_type_store import register_vehicle_type_adapter

    register_vehicle_type_adapter(OBJECT_REGISTRY)
    from app.arm2.vehicle_store import register_vehicle_adapter

    register_vehicle_adapter(OBJECT_REGISTRY)
    from app.arm2.counterparty_store import register_counterparty_adapter
    from app.arm2.organization_store import register_organization_adapter
    from app.arm2.representative_store import register_representative_adapters

    register_counterparty_adapter(OBJECT_REGISTRY)
    register_organization_adapter(OBJECT_REGISTRY)
    register_representative_adapters(OBJECT_REGISTRY)
    return OBJECT_REGISTRY
