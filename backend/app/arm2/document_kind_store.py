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
        return {"name": name}

    def create_record(self, db, values):
        record = DocumentKind(id=next_id(db, "DOC-KIND"), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        return db.scalar(select(DocumentKind).where(DocumentKind.id == object_id).with_for_update())

    def apply(self, record, values):
        record.name = values["name"]


DOCUMENT_KIND_ADAPTER = _DocumentKindAdapter()


def register_document_kind_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter("DOC-KIND", DOCUMENT_KIND_ADAPTER)


def create_document_kind(
    db: Session, name: str, *, principal: User, context: SaveContext,
) -> DocumentKind:
    """Create via the common SAVE transaction; the caller owns commit/rollback."""
    return create_object(
        db, object_type="DOC-KIND", values={"name": name}, principal=principal, context=context,
    ).record


def update_document_kind(
    db: Session, kind_id: str, expected_version: int, name: str, *, principal: User, context: SaveContext,
) -> DocumentKind:
    """Rename with the common conflict/no-op semantics; never replace the ID."""
    return update_object(
        db, object_type="DOC-KIND", object_id=kind_id, expected_version=expected_version,
        values={"name": name}, principal=principal, context=context,
    ).record
