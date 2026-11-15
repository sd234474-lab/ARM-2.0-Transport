"""DOC-KIND service contract; persistence/version/history belong to common SAVE."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2.document_kind_models import DocumentKind
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY
from app.arm2.object_save import (
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.models import User

DocumentKindNotFoundError = ObjectNotFoundError
DocumentKindVersionConflictError = ObjectVersionConflictError


class _DocumentKindAdapter:
    def normalize(self, values):
        name = values["name"].strip()
        if not name:
            raise ValueError("Document kind name must not be empty")
        parents = values["applicable_object_types"]
        connected = ("CP", "ORG", "VEH")
        if not parents or any(type(parent) is not str or parent not in connected for parent in parents):
            raise ValueError("Document kind parents must be a nonempty list drawn from CP, ORG, VEH")
        if len(set(parents)) != len(parents):
            raise ValueError("Document kind parents must not contain duplicates")
        maximum = values["max_current_documents"]
        if maximum is not None and (type(maximum) is not int or maximum <= 0):
            raise ValueError("max_current_documents must be a positive integer or None")
        return {"name": name, "applicable_object_types": [parent for parent in connected if parent in parents],
                "max_current_documents": maximum}

    def create_record(self, db, values):
        record = DocumentKind(id=next_id(db, "DOC-KIND"), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        return db.scalar(select(DocumentKind).where(DocumentKind.id == object_id).with_for_update())

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)


DOCUMENT_KIND_ADAPTER = _DocumentKindAdapter()


def register_document_kind_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter("DOC-KIND", DOCUMENT_KIND_ADAPTER)


def create_document_kind(
    db: Session, name: str, *, applicable_object_types: list[str], max_current_documents: int | None,
    principal: User, context: SaveContext,
) -> DocumentKind:
    """Create via the common SAVE transaction; the caller owns commit/rollback."""
    return create_object(
        db, object_type="DOC-KIND", values={"name": name, "applicable_object_types": applicable_object_types,
                                             "max_current_documents": max_current_documents},
        principal=principal, context=context,
    ).record


def update_document_kind(
    db: Session, kind_id: str, expected_version: int, name: str, *, applicable_object_types: list[str],
    max_current_documents: int | None, principal: User, context: SaveContext,
) -> DocumentKind:
    """Update name/settings with common conflict/no-op semantics; keep the ID."""
    return update_object(
        db, object_type="DOC-KIND", object_id=kind_id, expected_version=expected_version,
        values={"name": name, "applicable_object_types": applicable_object_types,
                "max_current_documents": max_current_documents}, principal=principal, context=context,
    ).record
