from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.arm2.history_store import append_history_event
from app.arm2.object_registry import (
    OBJECT_REGISTRY,
    ObjectRegistry,
    ObjectRegistryError,
    initialize_object_adapters,
)
from app.models import User
from app.permissions import Permission, has_permission


class InvalidExpectedVersionError(ValueError):
    pass


class ObjectNotFoundError(LookupError):
    pass


class ObjectVersionConflictError(RuntimeError):
    pass


class ObjectAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class SaveContext:
    actor_kind: str
    actor_id: str
    source: str
    correlation_id: str | None = None

    def validated(self) -> "SaveContext":
        values = {}
        for field in ("actor_kind", "actor_id", "source"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ObjectRegistryError(f"{field} must be a non-empty server-side value")
            values[field] = value.strip()
        correlation_id = self.correlation_id
        if correlation_id is not None:
            if not isinstance(correlation_id, str) or not correlation_id.strip():
                raise ObjectRegistryError("correlation_id must be non-empty when supplied")
            correlation_id = correlation_id.strip()
        return SaveContext(**values, correlation_id=correlation_id)


@dataclass(frozen=True)
class SaveResult:
    record: Any
    event: Any | None
    changed: bool


HistoryWriter = Callable[..., Any]


def _authorized_context(
    db: Session,
    definition,
    principal: User,
    context: SaveContext,
) -> SaveContext:
    if not isinstance(principal, User) or type(principal.id) is not int:
        raise ObjectAuthorizationError("A persisted User principal is required")
    permission = definition.edit_permission
    if not isinstance(permission, Permission):
        raise ObjectAuthorizationError(
            f"No edit permission is registered for {definition.object_type}"
        )
    with db.no_autoflush:
        current_user = db.get(User, principal.id, populate_existing=True)
    if current_user is None:
        raise ObjectAuthorizationError("User principal does not exist")
    if not current_user.active or current_user.deleted_at is not None:
        raise ObjectAuthorizationError("User principal is not active")
    if not has_permission(current_user, permission):
        raise ObjectAuthorizationError(
            f"Permission required: {permission}"
        )
    validated = context.validated()
    actor_id = str(current_user.id)
    if validated.actor_kind != "HUMAN" or validated.actor_id != actor_id:
        raise ObjectAuthorizationError("SaveContext author does not match principal")
    return SaveContext(
        actor_kind="HUMAN",
        actor_id=actor_id,
        source=validated.source,
        correlation_id=validated.correlation_id,
    )


def _definition(registry: ObjectRegistry, object_type: str):
    if registry is OBJECT_REGISTRY:
        initialize_object_adapters()
    definition = registry.require(object_type)
    if definition.adapter is None:
        raise ObjectRegistryError(f"Object type has no save adapter: {object_type}")
    return definition


def create_object(
    db: Session,
    *,
    object_type: str,
    values: dict[str, Any],
    principal: User,
    context: SaveContext,
    registry: ObjectRegistry = OBJECT_REGISTRY,
    history_writer: HistoryWriter = append_history_event,
) -> SaveResult:
    definition = _definition(registry, object_type)
    context = _authorized_context(db, definition, principal, context)
    if "CREATED" not in definition.operations:
        raise ObjectRegistryError(f"CREATED is not allowed for {object_type}")
    definition.validate_command(values)
    with db.begin_nested():
        normalized = definition.adapter.normalize(values)
        normalized = definition.apply_status(normalized)
        related_objects = registry.validate_references(definition, db, None, normalized)
        record = definition.adapter.create_record(db, normalized)
        definition.validate_id(record.id)
        snapshot = definition.snapshot(record)
        changes = {field: {"before": None, "after": value} for field, value in snapshot.items()}
        event = history_writer(
            db,
            object_type=object_type,
            object_id=record.id,
            operation="CREATED",
            actor_kind=context.actor_kind,
            actor_id=context.actor_id,
            source=context.source,
            correlation_id=context.correlation_id,
            version_after=record.version,
            changes=changes,
            related_objects=related_objects,
        )
    return SaveResult(record, event, True)


def update_object(
    db: Session,
    *,
    object_type: str,
    object_id: str,
    expected_version: int,
    values: dict[str, Any],
    principal: User,
    context: SaveContext,
    partial: bool = False,
    registry: ObjectRegistry = OBJECT_REGISTRY,
    history_writer: HistoryWriter = append_history_event,
) -> SaveResult:
    definition = _definition(registry, object_type)
    context = _authorized_context(db, definition, principal, context)
    if type(expected_version) is not int or expected_version <= 0:
        raise InvalidExpectedVersionError("expected_version must be a positive integer")
    if "UPDATED" not in definition.operations:
        raise ObjectRegistryError(f"UPDATED is not allowed for {object_type}")
    definition.validate_id(object_id)
    if partial:
        if type(values) is not dict:
            raise ObjectRegistryError("Object command must be a plain object")
        unknown = set(values) - set(definition.command_fields)
        if unknown:
            raise ObjectRegistryError(
                "Invalid command fields (unknown: " + ", ".join(sorted(unknown)) + ")"
            )
    else:
        definition.validate_command(values)
    with db.begin_nested():
        record = definition.adapter.lock_record(db, object_id)
        if record is None:
            raise ObjectNotFoundError(f"Object not found: {object_id}")
        if record.version != expected_version:
            raise ObjectVersionConflictError(
                f"Object version conflict: expected {expected_version}, current {record.version}"
            )
        if partial:
            merged = {
                field: getattr(record, field) for field in definition.command_fields
            }
            merged.update(values)
            values = merged
            definition.validate_command(values)
        before = definition.snapshot(record)
        normalized = definition.adapter.normalize(values)
        normalized = definition.apply_status(normalized)
        related_objects = registry.validate_references(
            definition, db, record, normalized
        )
        definition.adapter.apply(record, normalized)
        candidate = definition.snapshot(record)
        changed_fields = {
            field: {"before": before[field], "after": candidate[field]}
            for field in definition.history_fields
            if field != "version" and before[field] != candidate[field]
        }
        if not changed_fields:
            return SaveResult(record, None, False)
        record.version += 1
        db.flush()
        after = definition.snapshot(record)
        changed_fields["version"] = {"before": before["version"], "after": after["version"]}
        event = history_writer(
            db,
            object_type=object_type,
            object_id=object_id,
            operation="UPDATED",
            actor_kind=context.actor_kind,
            actor_id=context.actor_id,
            source=context.source,
            correlation_id=context.correlation_id,
            version_before=before["version"],
            version_after=after["version"],
            changes=changed_fields,
            related_objects=related_objects,
        )
    return SaveResult(record, event, True)
