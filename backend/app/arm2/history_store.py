from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.arm2.history_models import HistoryEvent
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectRegistryError


class HistoryValidationError(ValueError):
    pass


_OPERATIONS = {"CREATED", "UPDATED"}


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoryValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_version(value: int | None, field: str) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise HistoryValidationError(f"{field} must be a positive integer or None")
    return value


def _validate_object(object_type: str, object_id: str) -> tuple[str, str]:
    try:
        definition = OBJECT_REGISTRY.require(object_type)
        definition.validate_id(object_id)
    except ObjectRegistryError as error:
        raise HistoryValidationError(str(error)) from error
    return object_type, object_id


def _validate_changes(
    object_type: str,
    operation: str,
    changes: Mapping,
    version_before: int | None,
    version_after: int | None,
) -> dict:
    if type(changes) is not dict or not changes:
        raise HistoryValidationError("changes must be a non-empty plain object")
    definition = OBJECT_REGISTRY.require(object_type)
    allowed = set(definition.history_fields)
    unknown = set(changes) - allowed
    if unknown:
        raise HistoryValidationError(f"Unregistered history fields: {', '.join(sorted(unknown))}")
    if operation == "CREATED" and set(changes) != allowed:
        raise HistoryValidationError("CREATED must contain every registered field")
    if operation == "UPDATED" and "version" not in changes:
        raise HistoryValidationError("UPDATED must contain the version change")

    cleaned = {}
    for field, change in changes.items():
        if type(change) is not dict or set(change) != {"before", "after"}:
            raise HistoryValidationError(f"{field} change must contain only before and after")
        before, after = change["before"], change["after"]
        try:
            definition.history_fields[field].validate(after, field)
            if operation == "UPDATED":
                definition.history_fields[field].validate(before, field)
        except ObjectRegistryError as error:
            raise HistoryValidationError(str(error)) from error
        if operation == "CREATED" and before is not None:
            raise HistoryValidationError("CREATED before values must be None")
        if operation == "UPDATED" and before == after:
            raise HistoryValidationError("UPDATED may contain only changed fields")
        cleaned[field] = {"before": before, "after": after}

    version_change = cleaned["version"]
    if version_change != {"before": version_before, "after": version_after}:
        raise HistoryValidationError("Version change must match event versions")
    return cleaned


def _validate_related_objects(related_objects: list | None) -> list:
    if related_objects is None:
        return []
    if type(related_objects) is not list:
        raise HistoryValidationError("related_objects must be a list")
    cleaned = []
    for related in related_objects:
        if type(related) is not dict or set(related) != {"object_type", "object_id", "relation"}:
            raise HistoryValidationError("Each related object must contain type, id, and relation")
        object_type, object_id = _validate_object(related["object_type"], related["object_id"])
        cleaned.append(
            {
                "object_type": object_type,
                "object_id": object_id,
                "relation": _required_text(related["relation"], "relation"),
            }
        )
    return cleaned


def append_history_event(
    db: Session,
    *,
    object_type: str,
    object_id: str,
    operation: str,
    actor_kind: str,
    actor_id: str,
    source: str,
    changes: dict,
    version_before: int | None = None,
    version_after: int | None = None,
    correlation_id: str | None = None,
    related_objects: list | None = None,
) -> HistoryEvent:
    object_type, object_id = _validate_object(object_type, object_id)
    if operation not in _OPERATIONS:
        raise HistoryValidationError(f"Unknown operation: {operation}")
    version_before = _positive_version(version_before, "version_before")
    version_after = _positive_version(version_after, "version_after")
    if operation == "CREATED" and (version_before is not None or version_after != 1):
        raise HistoryValidationError("CREATED requires version_before=None and version_after=1")
    if operation == "UPDATED" and (
        version_before is None or version_after is None or version_after <= version_before
    ):
        raise HistoryValidationError("UPDATED requires increasing versions")
    cleaned_changes = _validate_changes(
        object_type,
        operation,
        changes,
        version_before,
        version_after,
    )
    event = HistoryEvent(
        object_type=object_type,
        object_id=object_id,
        operation=operation,
        actor_kind=_required_text(actor_kind, "actor_kind"),
        actor_id=_required_text(actor_id, "actor_id"),
        source=_required_text(source, "source"),
        correlation_id=(
            _required_text(correlation_id, "correlation_id") if correlation_id is not None else None
        ),
        version_before=version_before,
        version_after=version_after,
        changes=cleaned_changes,
        related_objects=_validate_related_objects(related_objects),
    )
    db.add(event)
    db.flush()
    return event
