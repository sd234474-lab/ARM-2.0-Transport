"""Zero-file logical documents. All writes/version/history use the common SAVE."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.arm2.counterparty_models import Counterparty
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
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
from app.models import User
from app.permissions import Permission, has_permission

PARENTS = {
    "CP": (Counterparty, Permission.ARM2_CP_VIEW, Permission.ARM2_CP_EDIT),
    "ORG": (Organization, Permission.ARM2_ORG_VIEW, Permission.ARM2_ORG_EDIT),
    "VEH": (Vehicle, Permission.ARM2_VEH_VIEW, Permission.ARM2_VEH_EDIT),
}


def parent_access(db, principal, parent_type, parent_id, *, edit=False):
    if parent_type not in PARENTS:
        raise ValueError("Родитель документов должен быть CP, ORG или VEH")
    if not isinstance(principal, User) or type(principal.id) is not int:
        raise ObjectAuthorizationError("A persisted user is required")
    with db.no_autoflush:
        user = db.get(User, principal.id, populate_existing=True)
    model, view, permission = PARENTS[parent_type]
    needed = [view, Permission.ARM2_DOCUMENT_VIEW]
    if edit:
        needed += [permission, Permission.ARM2_DOCUMENT_EDIT]
    if (
        user is None
        or not user.active
        or user.deleted_at is not None
        or any(not has_permission(user, p) for p in needed)
    ):
        raise ObjectAuthorizationError("Нет прав на документы этой родительской карточки")
    OBJECT_REGISTRY.require(parent_type).validate_id(parent_id)
    if db.get(model, parent_id) is None:
        raise ObjectNotFoundError("Родитель документа не найден")


def lock_kind(db, kind_id):
    OBJECT_REGISTRY.require("DOC-KIND").validate_id(kind_id)
    kind = db.scalar(
        select(DocumentKind)
        .where(DocumentKind.id == kind_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if kind is None:
        raise ValueError("Тип документа не найден")
    return kind


def document_relations(db, current, values):
    # Operations prelock both kinds in sorted order before the document lock.
    kind = lock_kind(db, values["kind_ref"])
    ptype, pid = values["parent_type"], values["parent_id"]
    if current is not None and ptype != current.parent_type:
        raise ValueError("Перенос между разными типами родителей запрещён")
    model = PARENTS[ptype][0]
    OBJECT_REGISTRY.require(ptype).validate_id(pid)
    if db.scalar(select(model.id).where(model.id == pid).with_for_update(read=True, key_share=True)) is None:
        raise ObjectNotFoundError("Родитель документа не найден")
    if values["archive_state"] == "Неархивный" and ptype not in kind.applicable_object_types:
        raise ValueError("Тип документа неприменим к этому родителю")
    count = db.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.kind_ref == kind.id,
            Document.parent_type == ptype,
            Document.parent_id == pid,
            Document.archive_state == "Неархивный",
            Document.id != (current.id if current is not None else ""),
        )
    )
    if (
        values["archive_state"] == "Неархивный"
        and kind.max_current_documents is not None
        and count + 1 > kind.max_current_documents
    ):
        raise ValueError("Превышено допустимое количество неархивных документов этого типа у родителя")
    if values["current_revision_id"] is not None:
        from app.arm2.document_file_models import DocumentRevision
        revision = db.get(DocumentRevision, values["current_revision_id"])
        if current is None or revision is None or revision.document_id != current.id:
            raise ValueError("Редакция не принадлежит документу")
    related = (
        ObjectReference(ptype, pid, "document_parent"),
        ObjectReference("DOC-KIND", kind.id, "document_kind"),
    )
    if current is not None and current.parent_id != pid:
        related += (ObjectReference(ptype, current.parent_id, "document_parent_from"),
                    ObjectReference(ptype, pid, "document_parent_to"))
    return related


def guard_kind_rules(db, current, values):
    if current is None or (
        current.applicable_object_types == values["applicable_object_types"]
        and current.max_current_documents == values["max_current_documents"]
    ):
        return ()
    # Common SAVE already holds this kind row FOR UPDATE. DOCUMENT uses the same lock.
    groups = db.execute(
        select(Document.parent_type, Document.parent_id, func.count())
        .where(Document.kind_ref == current.id, Document.archive_state == "Неархивный")
        .group_by(Document.parent_type, Document.parent_id)
    ).all()
    for ptype, _pid, count in groups:
        if ptype not in values["applicable_object_types"]:
            raise ValueError(f"Правила не сохранены: существуют неархивные документы у родителя типа {ptype}")
        if values["max_current_documents"] is not None and count > values["max_current_documents"]:
            raise ValueError(
                "Правила не сохранены: новый лимит меньше числа неархивных документов у одного родителя"
            )
    return ()


class _DocumentAdapter:
    def normalize(self, values):
        if values["parent_type"] not in PARENTS:
            raise ValueError("Неподключённый тип родителя")
        name = values["name"].strip()
        if not name:
            raise ValueError("Укажите наименование документа")
        if values["archive_state"] not in {"Неархивный", "Архивный"}:
            raise ValueError("Недопустимая архивность документа")
        return {**values, "name": name}

    def create_record(self, db, values):
        record = Document(id=next_id(db, "DOCUMENT"), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        kind_id = db.scalar(select(Document.kind_ref).where(Document.id == object_id))
        if kind_id is None:
            return None
        lock_kind(db, kind_id)
        record = db.scalar(
            select(Document)
            .where(Document.id == object_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is not None and record.kind_ref != kind_id:
            raise ObjectVersionConflictError("Тип документа изменился во время ожидания; повторите запрос")
        return record

    def apply(self, record, values):
        record.name, record.description = values["name"], values["description"]
        record.current_revision_id = values["current_revision_id"]
        record.parent_id, record.kind_ref = values["parent_id"], values["kind_ref"]
        record.archive_state = values["archive_state"]


ADAPTER = _DocumentAdapter()


def register_document_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter("DOCUMENT", ADAPTER, reference_rules=(document_relations,))


def create_document(
    db: Session,
    parent_type: str,
    parent_id: str,
    kind_ref: str,
    name: str,
    description: str | None,
    *,
    principal: User,
    context: SaveContext,
):
    parent_access(db, principal, parent_type, parent_id, edit=True)
    return create_object(
        db,
        object_type="DOCUMENT",
        values={
            "parent_type": parent_type,
            "parent_id": parent_id,
            "kind_ref": kind_ref,
            "name": name,
            "description": description,
            "current_revision_id": None,
            "archive_state": "Неархивный",
        },
        principal=principal,
        context=context,
    ).record


def update_document(
    db: Session,
    parent_type: str,
    parent_id: str,
    document_id: str,
    expected_version: int,
    name: str,
    description: str | None,
    *,
    principal: User,
    context: SaveContext,
):
    parent_access(db, principal, parent_type, parent_id, edit=True)
    identity = db.execute(
        select(Document.parent_type, Document.parent_id).where(Document.id == document_id)
    ).one_or_none()
    if identity is None or identity != (parent_type, parent_id):
        raise ObjectNotFoundError("Документ этой карточки не найден")
    lock_document_operation(db, principal, parent_type, parent_id, document_id)
    return update_object(
        db,
        object_type="DOCUMENT",
        object_id=document_id,
        expected_version=expected_version,
        values={"name": name, "description": description},
        partial=True,
        principal=principal,
        context=context,
    ).record


def lock_document_operation(db, user, ptype, pid, docid, *, target_kind=None, target_parent=None):
    """Kinds sorted -> parent key-share locks sorted -> document. Recheck after waiting."""
    parent_access(db, user, ptype, pid, edit=True)
    identity = db.execute(select(Document.kind_ref, Document.parent_type, Document.parent_id)
                          .where(Document.id == docid)).one_or_none()
    if identity is None or identity[1:] != (ptype, pid):
        raise ObjectNotFoundError("Документ этой карточки не найден")
    kinds = sorted({identity.kind_ref, target_kind or identity.kind_ref})
    for kind in kinds:
        lock_kind(db, kind)
    for parent_id in sorted({pid, target_parent or pid}):
        parent_access(db, user, ptype, parent_id, edit=True)
        model = PARENTS[ptype][0]
        db.scalar(select(model.id).where(model.id == parent_id).with_for_update(read=True, key_share=True))
    record = db.scalar(select(Document).where(Document.id == docid).with_for_update()
                       .execution_options(populate_existing=True))
    if record is None or (record.kind_ref, record.parent_type, record.parent_id) != tuple(identity):
        raise ObjectVersionConflictError("Привязка документа изменилась во время ожидания")
    for parent_id in {pid, target_parent or pid}:
        parent_access(db, user, ptype, parent_id, edit=True)
    return record


def operate_document(db, ptype, pid, docid, expected_version, operation, *, principal, context,
                     kind_ref=None, target_type=None, target_id=None):
    if operation not in {"archive", "restore", "retype", "move"}:
        raise ValueError("Неизвестная операция документа")
    if operation == "move" and target_type != ptype:
        raise ValueError("Перенос между разными типами родителей запрещён")
    if operation == "retype" and not kind_ref:
        raise ValueError("Выберите тип документа")
    if operation == "move" and not target_id:
        raise ValueError("Выберите получателя")
    record = lock_document_operation(db, principal, ptype, pid, docid,
                                     target_kind=kind_ref if operation == "retype" else None,
                                     target_parent=target_id if operation == "move" else None)
    values = ({"archive_state": "Архивный" if operation == "archive" else "Неархивный"}
              if operation in {"archive", "restore"} else
              {"kind_ref": kind_ref} if operation == "retype" else {"parent_id": target_id})
    # Applicability is checked for RETYPE/MOVE even while the record is archived.
    selected_kind = db.get(DocumentKind, kind_ref or record.kind_ref)
    if operation in {"retype", "move"} and ptype not in selected_kind.applicable_object_types:
        raise ValueError("Тип документа неприменим к этому родителю")
    if operation == "retype":
        from app.arm2.document_file_models import DocumentFile, DocumentFileCorrection
        from app.arm2.document_file_storage import safe_name

        readable = db.scalar(select(DocumentFile).where(DocumentFile.document_id == docid,
                             DocumentFile.id.not_in(select(DocumentFileCorrection.file_id))).limit(1))
        if readable is not None:
            # Reuse B3-C naming validation. Never commit a type which breaks existing downloads.
            safe_name(selected_kind.file_naming_template, record, db.get(PARENTS[ptype][0], pid),
                      selected_kind, readable.id, readable.extension)
    return update_object(db, object_type="DOCUMENT", object_id=docid,
                         expected_version=expected_version, values=values, partial=True,
                         principal=principal, context=context).record
