"""Incoming physical and live logical references for the ARM2 object registry."""

from dataclasses import dataclass

from sqlalchemy import inspect, select, text, tuple_
from sqlalchemy.orm import Session

from app.arm2.approval_models import ApprovalAttempt, ApprovalDecision, ApprovalRequest
from app.arm2.bulk_models import BulkReceipt, BulkSession
from app.arm2.counterparty_models import Counterparty
from app.arm2.document_file_models import (
    DocumentFile,
    DocumentFileCorrection,
    DocumentRevision,
    DocumentRevisionContext,
    FileAcceptance,
    FileStage,
    RevisionFile,
)
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.object_registry import OBJECT_REGISTRY
from app.arm2.object_save import ObjectVersionConflictError
from app.arm2.organization_models import Organization
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireExport, QuestionnaireUpload
from app.arm2.representative_models import CounterpartyRepresentative, OrganizationRepresentative
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType


@dataclass(frozen=True)
class DeletionType:
    model: type
    collection: str


OBJECTS = {
    "TYPE": DeletionType(VehicleType, "/arm2/type"),
    "VEH": DeletionType(Vehicle, "/arm2/veh"),
    "CP": DeletionType(Counterparty, "/arm2/cp"),
    "ORG": DeletionType(Organization, "/arm2/org"),
    "CPREP": DeletionType(CounterpartyRepresentative, "/arm2/cp"),
    "ORGREP": DeletionType(OrganizationRepresentative, "/arm2/org"),
    "DOC-KIND": DeletionType(DocumentKind, "/arm2/document-kinds"),
    "DOCUMENT": DeletionType(Document, "/arm2"),
    "APPROVAL": DeletionType(ApprovalRequest, "/arm2/approvals"),
    "APPROVAL-ITEM": DeletionType(ApprovalDecision, "/arm2/approvals"),
}
OBJECT_REGISTRY.register_deletion(OBJECTS)


# These records are removed only with their owner. Staging/reservations have a
# separate liveness check before they can be retired.
ARTIFACTS = {
    cls: "document_id" for cls in (
        DocumentRevision, DocumentRevisionContext, RevisionFile, DocumentFileCorrection, FileAcceptance, FileStage
    )
}
ARTIFACTS[DocumentFile] = "document_id"

ARTIFACT_NAMES = {
    DocumentFile: "Оригинал",
    DocumentRevision: "Редакция",
    DocumentRevisionContext: "Историческая привязка редакции",
    RevisionFile: "Файл редакции",
    DocumentFileCorrection: "Исправление загрузки",
    FileAcceptance: "Приёмка комплекта",
    FileStage: "Временная загрузка",
}

HISTORICAL_FK = {
    BulkReceipt: ("Квитанция импорта техники", "command_id", "vehicle_id"),
    QuestionnaireExport: ("Протокол выгрузки анкеты", "id", "cp_id"),
}

# Supported columns in open workspaces; receipts, completed attempts, and
# history retain IDs as audit values independent of active relations.
LOGICAL = {
    BulkSession: (("payload",), BulkSession.id, ()),
    QuestionnaireDraft: (("choices", "preview"), QuestionnaireDraft.id,
                         (QuestionnaireDraft.status == "OPEN",)),
    ApprovalAttempt: (("plan", "target_id"), ApprovalAttempt.id,
                      (ApprovalAttempt.result.is_(None), ApprovalAttempt.action != "response-stage")),
    ApprovalRequest: (("vehicles_snapshot", "response_file_ids"), ApprovalRequest.id, ()),
}

SOURCE_TYPES = {entry.model.__tablename__: kind for kind, entry in OBJECTS.items()}
MODELS = {cls.__tablename__: cls for cls in (
    *(item.model for item in OBJECTS.values()), *ARTIFACTS, *HISTORICAL_FK, *LOGICAL, QuestionnaireUpload
)}


def lock_writes(db: Session):
    db.execute(text("SET LOCAL lock_timeout = '4s'"))
    # Row locks alone allow new JSON references. Lock only the registered ARM2 relationships
    # while reading the preflight/committing the removal, with no provider calls inside.
    db.execute(text("LOCK TABLE " + ", ".join(sorted(MODELS)) + " IN SHARE ROW EXCLUSIVE MODE"))


def object_key(row):
    return SOURCE_TYPES[row.__tablename__], row.id


def owner_key(row):
    if isinstance(row, DocumentFile) and row.process_id:
        return "APPROVAL", row.process_id
    return "DOCUMENT", getattr(row, ARTIFACTS[type(row)])


def card_path(kind, row):
    base = OBJECTS[kind].collection
    if kind == "DOCUMENT":
        return f"{base}/{row.parent_type.lower()}/{row.parent_id}/documents/{row.id}"
    if kind in {"CPREP", "ORGREP"}:
        return f"{base}/{row.parent_id}/representatives/{row.id}"
    if kind == "APPROVAL-ITEM":
        return f"{base}/{row.request_id}#{row.id}"
    return f"{base}/{row.id}"


def artifact_id(row):
    if isinstance(row, RevisionFile):
        return f"{row.revision_id}:{row.file_id}"
    if isinstance(row, DocumentRevisionContext):
        return row.revision_id
    if isinstance(row, DocumentFileCorrection):
        return row.file_id
    return row.id


def artifact_rows(db, selected):
    document_ids = [row.id for row in selected if isinstance(row, Document)]
    request_ids = [row.id for row in selected if isinstance(row, ApprovalRequest)]
    for model, field in ARTIFACTS.items():
        matches = getattr(model, field).in_(document_ids)
        if model is DocumentFile:
            matches |= model.process_id.in_(request_ids)
        yield from db.scalars(select(model).where(matches))


def incoming_foreign_keys(db, targets):
    """Discover every physical incoming reference, including compound keys."""
    tables = {}
    for row in targets:
        tables.setdefault(row.__tablename__, []).append(row)
    external = db.execute(text("""
        SELECT n.nspname, r.relname, c.conname
        FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=r.relnamespace
        JOIN pg_class t ON t.oid=c.confrelid
        JOIN pg_namespace tn ON tn.oid=t.relnamespace
        WHERE c.contype='f' AND tn.nspname='public' AND n.nspname <> 'public'
        AND t.relname=ANY(CAST(:tables AS text[]))
        ORDER BY n.nspname, r.relname, c.conname LIMIT 1
    """), {"tables": sorted(tables)}).first()
    if external:
        raise ObjectVersionConflictError(f"Нет зарегистрированной политики для связи {'.'.join(external)}")
    inspector = inspect(db.connection())
    for (schema, table), keys in inspector.get_multi_foreign_keys(schema="public").items():
        for fk in keys:
            referenced = fk["referred_table"]
            if referenced not in tables or fk["referred_schema"] not in ("public", None):
                continue
            model = MODELS.get(table)
            if model is None:
                raise ObjectVersionConflictError(f"Нет зарегистрированной политики для связи {schema}.{table}")
            source = model.__table__
            known = any(
                tuple(col.name for col in rule.columns) == tuple(fk["constrained_columns"])
                and tuple(item.target_fullname for item in rule.elements)
                == tuple(f"{referenced}.{col}" for col in fk["referred_columns"])
                for rule in source.foreign_key_constraints
            )
            if not known:
                raise ObjectVersionConflictError(f"Нет зарегистрированной политики для связи {table}.{fk['name']}")
            columns = [source.c[col] for col in fk["constrained_columns"]]
            referenced_rows = {
                tuple(getattr(target, target.__table__.c[col].key) for col in fk["referred_columns"]): target
                for target in tables[referenced]
            }
            if len(columns) == 1:
                predicate = columns[0].in_([key[0] for key in referenced_rows])
            else:
                predicate = tuple_(*columns).in_(list(referenced_rows))
            field = "parent_id" if columns[0].name in {"_cp", "_org", "_veh"} else columns[0].name
            for row in db.scalars(select(model).where(predicate)):
                key = tuple(getattr(row, col.key) for col in columns)
                yield row, referenced_rows[key], field


# Option pools, raw upload cells and audit baselines are snapshots/suggestions.
# They do not designate a current target and are recalculated by the consumers.
PASSIVE_FIELDS = frozenset({
    "types", "document_kinds", "candidates", "attachment_options", "source",
    "rows", "originals", "warnings", "failures",
})


def value_refs(value, selected_ids):
    if isinstance(value, dict):
        for key, field in value.items():
            if key in PASSIVE_FIELDS:
                continue
            if key in selected_ids:
                yield key
            yield from value_refs(field, selected_ids)
    elif isinstance(value, list):
        for item in value:
            yield from value_refs(item, selected_ids)
    elif isinstance(value, str) and value in selected_ids:
        yield value


def incoming_logical(db, targets):
    by_id = {row.id: row for row in targets if hasattr(row, "id")}
    for model, (fields, id_column, conditions) in LOGICAL.items():
        for row in db.scalars(select(model).where(*conditions).order_by(id_column)):
            for field in fields:
                for identifier in sorted(set(value_refs(getattr(row, field), by_id))):
                    yield row, by_id[identifier], field
