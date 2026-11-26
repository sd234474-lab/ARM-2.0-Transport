from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any, Callable, Protocol

from sqlalchemy import Table, inspect
from sqlalchemy.orm import Mapper, Session

from app.arm2.ids import is_canonical_id
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
    server_fields: dict[str, FieldSpec] = dataclass_field(default_factory=dict)

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
        if not is_canonical_id(self.id_prefix, object_id):
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


@dataclass(frozen=True)
class DeletionPolicy:
    model: type
    order: int
    clear_fields: tuple[str, ...] = ()

    def validate(self, *, logical_object: bool = False) -> None:
        mapper = inspect(self.model, raiseerr=False)
        if (not isinstance(mapper, Mapper) or not isinstance(mapper.local_table, Table)
                or mapper.persist_selectable is not mapper.local_table or not mapper.local_table.primary_key.columns
                or type(self.order) is not int or self.order < 1):
            raise ObjectRegistryError("Не определена политика удаления для зарегистрированной модели")
        table = mapper.local_table
        if logical_object and (list(table.primary_key.columns.keys()) != ["id"] or "version" not in table.c):
            raise ObjectRegistryError(f"У {table.name} отсутствуют ID или версия объекта")
        if not isinstance(self.clear_fields, tuple) or any(not isinstance(name, str) for name in self.clear_fields):
            raise ObjectRegistryError(f"Некорректная политика разрыва внутренних связей {table.name}")
        if len(set(self.clear_fields)) != len(self.clear_fields) or any(
            name not in table.c or not table.c[name].nullable or table.c[name].primary_key
            for name in self.clear_fields
        ):
            raise ObjectRegistryError(f"Некорректная политика разрыва внутренних связей {table.name}")


@dataclass(frozen=True)
class DeletionType(DeletionPolicy):
    collection: str = ""

    def validate(self, *, logical_object: bool = True) -> None:
        super().validate(logical_object=logical_object)
        if not isinstance(self.collection, str) or not (self.collection == "/arm2" or self.collection.startswith("/arm2/")):
            raise ObjectRegistryError("Не указан маршрут карточки зарегистрированного объекта")


class ObjectRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ObjectDefinition] = {}
        self._deletion: dict[str, DeletionType] = {}

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

    def register_deletion(self, definitions: dict[str, DeletionType]) -> None:
        if self._deletion:
            raise ObjectRegistryError("Политики удаления уже зарегистрированы")
        self._validate_deletion(definitions)
        self._deletion = definitions.copy()

    def _validate_deletion(self, definitions: dict[str, DeletionType]) -> None:
        if set(definitions) != set(self._definitions):
            raise ObjectRegistryError("Не для всех типов объектов определена политика удаления")
        tables = set()
        for definition in definitions.values():
            if not isinstance(definition, DeletionType):
                raise ObjectRegistryError("Не определена политика удаления для зарегистрированного типа")
            definition.validate()
            table = definition.model.__table__.key
            if table in tables:
                raise ObjectRegistryError(f"Конфликт политик удаления для {table}")
            tables.add(table)

    def deletion_definitions(self) -> dict[str, DeletionType]:
        self._validate_deletion(self._deletion)
        return self._deletion.copy()

    def deletion_type_for(self, model: type) -> str | None:
        return next((kind for kind, definition in self._deletion.items() if definition.model is model), None)

    def deletion_definition(self, object_type: str) -> DeletionType:
        self.require(object_type)
        if object_type not in self._deletion:
            raise ObjectRegistryError(f"Не определена политика удаления для {object_type}")
        return self._deletion[object_type]

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
    "plate_category": FieldSpec((str,), nullable=True),
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
DOCUMENT_FIELDS = {"parent_type": FieldSpec((str,)), "parent_id": FieldSpec((str,)), "kind_ref": FieldSpec((str,)),
                   "name": FieldSpec((str,)), "description": FieldSpec((str,), nullable=True), "current_revision_id": FieldSpec((str,), nullable=True), "archive_state": FieldSpec((str,))}
OBJECT_REGISTRY.register(ObjectDefinition("DOCUMENT", "DOCUMENT", DOCUMENT_FIELDS,
    {**DOCUMENT_FIELDS, "archive_state": FieldSpec((str,)), "version": FieldSpec((int,))},
    edit_permission=Permission.ARM2_DOCUMENT_EDIT))
OBJECT_REGISTRY.register(
    ObjectDefinition(
        "DOC-KIND", "DOC-KIND",
        {"name": FieldSpec((str,)), "applicable_object_types": FieldSpec((list,)),
         "max_current_documents": FieldSpec((int,), nullable=True), "file_naming_template": FieldSpec((str,), nullable=True)},
        {"name": FieldSpec((str,)), "version": FieldSpec((int,)),
         "applicable_object_types": FieldSpec((list,)), "max_current_documents": FieldSpec((int,), nullable=True), "file_naming_template": FieldSpec((str,), nullable=True)},
        edit_permission=Permission.ARM2_DOC_KIND_EDIT,
    )
)
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
PARTY_HISTORY_FIELDS = {**PARTY_COMMAND_FIELDS, "version": FieldSpec((int,))}
CP_NAME_FIELDS = {"type": FieldSpec((str,)), **{f: FieldSpec((str,), nullable=True) for f in ("last_name", "first_name", "middle_name")}}
OBJECT_REGISTRY.register(ObjectDefinition("CP", "CP", {**PARTY_COMMAND_FIELDS, **CP_NAME_FIELDS}, {**PARTY_HISTORY_FIELDS, **CP_NAME_FIELDS},
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
    "calibration_m3": FieldSpec((float,), nullable=True),
    "calibration_confirmed_at": FieldSpec((datetime,), nullable=True),
    "calibration_confirmed_by": FieldSpec((int,), nullable=True),
    "calibration_document_id": FieldSpec((str,), nullable=True),
    "calibration_revision_id": FieldSpec((str,), nullable=True),
    "calibration_plate_number": FieldSpec((str,), nullable=True),
}
OBJECT_REGISTRY.register(
    ObjectDefinition(
        "VEH",
        "VEH",
        VEH_COMMAND_FIELDS,
        VEH_HISTORY_FIELDS,
        edit_permission=Permission.ARM2_VEH_EDIT,
        server_fields={key: value for key, value in VEH_HISTORY_FIELDS.items()
                       if key.startswith("calibration_")},
    )
)


APPROVAL_FIELDS = {
    "name": FieldSpec((str,)), "contract_number_1": FieldSpec((str,)), "contract_number_2": FieldSpec((str,)),
    "vehicles_snapshot": FieldSpec((list,)), "letter_file_id": FieldSpec((str,)),
    "package_file_id": FieldSpec((str,)), "response_file_ids": FieldSpec((list,)),
    "last_response_at": FieldSpec((datetime,), nullable=True),
    "created_at": FieldSpec((datetime,)), "author_id": FieldSpec((int,)),
}
DECISION_FIELDS = {
    "request_id": FieldSpec((str,)), "vehicle_id": FieldSpec((str,)), "plate_number": FieldSpec((str,)),
    "volume_m3": FieldSpec((float,)), "state": FieldSpec((str,)),
    "response_file_id": FieldSpec((str,), nullable=True), "decided_at": FieldSpec((datetime,), nullable=True),
    "decided_by": FieldSpec((int,), nullable=True),
}
OBJECT_REGISTRY.register(ObjectDefinition("APPROVAL", "APPROVAL", APPROVAL_FIELDS,
    {**APPROVAL_FIELDS, "version": FieldSpec((int,))}, edit_permission=Permission.ARM2_VEH_EDIT))
OBJECT_REGISTRY.register(ObjectDefinition("APPROVAL-ITEM", "APPROVAL-ITEM", DECISION_FIELDS,
    {**DECISION_FIELDS, "version": FieldSpec((int,))}, edit_permission=Permission.ARM2_VEH_EDIT))


def initialize_object_adapters() -> ObjectRegistry:
    """Idempotently attach every production adapter to the shared registry."""
    from app.arm2.document_store import register_document_adapter

    register_document_adapter(OBJECT_REGISTRY)
    from app.arm2.document_kind_store import register_document_kind_adapter

    register_document_kind_adapter(OBJECT_REGISTRY)
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
    from app.arm2.approval_store import register_approval_adapters

    register_approval_adapters(OBJECT_REGISTRY)
    return OBJECT_REGISTRY
