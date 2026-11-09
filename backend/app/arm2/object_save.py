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


class InvalidExpectedVersionError(ValueError):
    pass


class ObjectNotFoundError(LookupError):
    pass


class ObjectVersionConflictError(RuntimeError):
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
    context: SaveContext,
    registry: ObjectRegistry = OBJECT_REGISTRY,
    history_writer: HistoryWriter = append_history_event,
) -> SaveResult:
    definition = _definition(registry, object_type)
    if "CREATED" not in definition.operations:
        raise ObjectRegistryError(f"CREATED is not allowed for {object_type}")
    definition.validate_command(values)
    context = context.validated()
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
    context: SaveContext,
    registry: ObjectRegistry = OBJECT_REGISTRY,
    history_writer: HistoryWriter = append_history_event,
) -> SaveResult:
    if type(expected_version) is not int or expected_version <= 0:
        raise InvalidExpectedVersionError("expected_version must be a positive integer")
    definition = _definition(registry, object_type)
    if "UPDATED" not in definition.operations:
        raise ObjectRegistryError(f"UPDATED is not allowed for {object_type}")
    definition.validate_id(object_id)
    definition.validate_command(values)
    context = context.validated()
    with db.begin_nested():
        record = definition.adapter.lock_record(db, object_id)
        if record is None:
            raise ObjectNotFoundError(f"Object not found: {object_id}")
        if record.version != expected_version:
            raise ObjectVersionConflictError(
                f"Object version conflict: expected {expected_version}, current {record.version}"
            )
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
