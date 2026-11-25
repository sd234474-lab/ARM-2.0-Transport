"""Incoming physical and live logical references for the ARM2 object registry."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import inspect, or_, select, text, tuple_
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
from app.arm2.document_file_storage import STAGE_SECONDS
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.object_registry import OBJECT_REGISTRY, DeletionPolicy, DeletionType, ObjectRegistryError
from app.arm2.object_save import ObjectVersionConflictError
from app.arm2.operation_models import BackgroundOperation
from app.arm2.organization_models import Organization
from app.arm2.questionnaire_models import (
    QuestionnaireDraft,
    QuestionnaireExport,
    QuestionnaireReceipt,
    QuestionnaireUpload,
)
from app.arm2.reference_paths import (
    approval_attempt,
    approval_outcome,
    approval_snapshot,
    bulk_selection,
    file_acceptance,
    questionnaire_job,
    questionnaire_outcome,
    questionnaire_pending_targets,
    questionnaire_selection,
    questionnaire_upload,
)
from app.arm2.representative_models import CounterpartyRepresentative, OrganizationRepresentative
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType

OBJECT_REGISTRY.register_deletion({
    "TYPE": DeletionType(VehicleType, 160, collection="/arm2/type"),
    "VEH": DeletionType(Vehicle, 110, collection="/arm2/veh", clear_fields=(
        "calibration_m3", "calibration_confirmed_by", "calibration_confirmed_at",
        "calibration_document_id", "calibration_revision_id", "calibration_plate_number",
    )),
    "CP": DeletionType(Counterparty, 140, collection="/arm2/cp", clear_fields=("primary_signer_id",)),
    "ORG": DeletionType(Organization, 150, collection="/arm2/org", clear_fields=("primary_signer_id",)),
    "CPREP": DeletionType(CounterpartyRepresentative, 120, collection="/arm2/cp"),
    "ORGREP": DeletionType(OrganizationRepresentative, 130, collection="/arm2/org"),
    "DOC-KIND": DeletionType(DocumentKind, 155, collection="/arm2/document-kinds"),
    "DOCUMENT": DeletionType(Document, 100, collection="/arm2", clear_fields=("current_revision_id",)),
    "APPROVAL": DeletionType(ApprovalRequest, 90, collection="/arm2/approvals"),
    "APPROVAL-ITEM": DeletionType(ApprovalDecision, 10, collection="/arm2/approvals"),
})


@dataclass(frozen=True)
class DeletionArtifact(DeletionPolicy):
    name: str = ""
    owners: tuple[tuple[str, str], ...] = (("DOCUMENT", "document_id"),)
    key_fields: tuple[str, ...] = ("id",)
    active: Callable | None = None
    eligible: tuple = ()
    lease: Callable | None = None
    private: bool = False
    cleanup: Callable | None = None

    def validate(self, *, logical_object: bool = False) -> None:
        super().validate(logical_object=logical_object)
        if not self.name or not self.owners or any(
            field not in self.model.__table__.c for _, field in self.owners
        ) or any(field not in self.model.__table__.c for field in self.key_fields):
            raise ObjectRegistryError("Не определён владелец технической записи удаления")


def is_active_stage(row, now):
    return row.expires_at > now and row.state != "consumed"


def is_active_acceptance(row, now):
    return row.expires_at > now and row.state != "accepted"


def is_active_attempt(row, now):
    return row.result is None and row.created_at + timedelta(seconds=STAGE_SECONDS) > now


def document_original(row):
    return (), ({key: getattr(row, key) for key in ("id", "storage_ref", "sha256", "size")},)


def unpublished_acceptance(row):
    return (), tuple(item for item in row.plan["files"] if not item["existing"])


def reserved_approval_originals(row):
    items = row.plan["files"] if row.action == "letter" else [row.plan["file"]]
    return tuple((item["token"], item["id"]) for item in items), items


# These records are removed only with their owner. Staging/reservations have a
# separate liveness check before they can be retired.
ARTIFACTS = {policy.model: policy for policy in (
    DeletionArtifact(DocumentFileCorrection, 20, name="Исправление загрузки", key_fields=("file_id",)),
    DeletionArtifact(RevisionFile, 30, name="Файл редакции", key_fields=("revision_id", "file_id")),
    DeletionArtifact(DocumentRevisionContext, 40, name="Историческая привязка редакции", key_fields=("revision_id",)),
    DeletionArtifact(FileAcceptance, 50, name="Приёмка комплекта", active=is_active_acceptance,
                     lease=lambda row: row.id, private=True, cleanup=unpublished_acceptance),
    DeletionArtifact(FileStage, 60, name="Временная загрузка", active=is_active_stage, private=True,
                     cleanup=lambda row: (((row.id, row.file_id),), ())),
    DeletionArtifact(DocumentRevision, 70, name="Редакция"),
    DeletionArtifact(DocumentFile, 80, name="Оригинал", owners=(("DOCUMENT", "document_id"), ("APPROVAL", "process_id")),
                     cleanup=document_original),
    DeletionArtifact(ApprovalAttempt, 170, name="Черновик ответа заказчика",
                     owners=(("APPROVAL", "target_id"),), active=is_active_attempt,
                     eligible=(ApprovalAttempt.action == "response-stage",),
                     lease=lambda row: "approval-" + row.id, private=True, cleanup=reserved_approval_originals),
)}


class Purpose(StrEnum):
    ACTIVE_DOMAIN = "ACTIVE_DOMAIN"
    OPEN_OPERATION = "OPEN_OPERATION"
    HISTORY = "HISTORY"
    OWNED = "OWNED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReferencePolicy:
    source: type
    purpose: Purpose
    target: type | None = None
    columns: tuple[str, ...] = ()
    target_columns: tuple[str, ...] = ("id",)
    path: str = ""
    extract: Callable | None = None
    conditions: tuple = ()
    label: str = ""
    author: str = ""
    date: str = ""
    url: Callable | None = None
    permissions: tuple[str, ...] = ()
    active: Callable | None = None
    retire: Callable | None = None
    origin: Callable | None = None

    def signature(self):
        return (self.source.__tablename__, self.columns, self.target.__tablename__, self.target_columns)

    def validate(self):
        if self.columns:
            if self.extract or self.target is None or len(self.columns) != len(self.target_columns):
                raise ObjectRegistryError("Некорректная политика внешнего ключа")
            if (any(key not in self.source.__table__.c for key in self.columns)
                    or any(key not in self.target.__table__.c for key in self.target_columns)):
                raise ObjectRegistryError("Политика ссылки указывает на неизвестную колонку")
        elif not self.path or not self.extract:
            raise ObjectRegistryError("Не определён типизированный путь логической ссылки")
        if self.purpose == Purpose.OWNED and self.source not in ARTIFACTS and self.target not in ARTIFACTS:
            raise ObjectRegistryError("Не определена политика удаления артефакта")
        if self.retire is not None and (self.active is None or self.source not in ARTIFACTS):
            raise ObjectRegistryError("Не определено условие завершения операции")


@dataclass(frozen=True)
class IncomingReference:
    policy: ReferencePolicy
    source: Any
    target: Any
    field: str
    retired: bool = False


def fk(source, fields, target, target_fields="id", *, purpose=Purpose.ACTIVE_DOMAIN):
    return ReferencePolicy(source, purpose, target, tuple(fields.split()), tuple(target_fields.split()))


PHYSICAL = (
    fk(Vehicle, "type_id", VehicleType),
    fk(Vehicle, "owner_cp_id", Counterparty),
    fk(Vehicle, "owner_org_id", Organization),
    fk(CounterpartyRepresentative, "parent_id", Counterparty),
    fk(OrganizationRepresentative, "parent_id", Organization),
    fk(Counterparty, "primary_signer_id id", CounterpartyRepresentative, "id parent_id"),
    fk(Organization, "primary_signer_id id", OrganizationRepresentative, "id parent_id"),
    fk(Document, "kind_ref", DocumentKind),
    fk(Document, "_cp", Counterparty),
    fk(Document, "_org", Organization),
    fk(Document, "_veh", Vehicle),
    fk(Document, "current_revision_id id", DocumentRevision, "id document_id", purpose=Purpose.OWNED),
    fk(Vehicle, "calibration_document_id", Document),
    fk(Vehicle, "calibration_revision_id", DocumentRevision),
    fk(Vehicle, "calibration_revision_id calibration_document_id", DocumentRevision, "id document_id"),
    fk(DocumentRevision, "document_id", Document, purpose=Purpose.OWNED),
    fk(DocumentFile, "document_id", Document, purpose=Purpose.OWNED),
    fk(DocumentFile, "process_id", ApprovalRequest, purpose=Purpose.OWNED),
    fk(DocumentRevisionContext, "revision_id document_id", DocumentRevision, "id document_id", purpose=Purpose.OWNED),
    fk(RevisionFile, "revision_id document_id", DocumentRevision, "id document_id", purpose=Purpose.OWNED),
    fk(RevisionFile, "file_id document_id", DocumentFile, "id document_id", purpose=Purpose.OWNED),
    fk(DocumentFileCorrection, "file_id document_id", DocumentFile, "id document_id", purpose=Purpose.OWNED),
    fk(FileStage, "document_id", Document, purpose=Purpose.OWNED),
    fk(FileAcceptance, "document_id", Document, purpose=Purpose.OWNED),
    fk(ApprovalRequest, "letter_file_id", DocumentFile, purpose=Purpose.OWNED),
    fk(ApprovalRequest, "letter_file_id id", DocumentFile, "id process_id", purpose=Purpose.OWNED),
    fk(ApprovalRequest, "package_file_id", DocumentFile, purpose=Purpose.OWNED),
    fk(ApprovalRequest, "package_file_id id", DocumentFile, "id process_id", purpose=Purpose.OWNED),
    fk(ApprovalDecision, "request_id", ApprovalRequest),
    fk(ApprovalDecision, "vehicle_id", Vehicle),
    fk(ApprovalDecision, "response_file_id", DocumentFile),
    fk(ApprovalDecision, "response_file_id request_id", DocumentFile, "id process_id"),
)


LOGICAL = (
    ReferencePolicy(BulkSession, Purpose.OPEN_OPERATION, path="payload.plans", extract=bulk_selection,
                    label="Черновик загрузки техники", author="user_id", date="created_at",
                    url=lambda row: f"/arm2/veh/bulk?session={row.id}", permissions=("arm2_veh_edit",)),
    ReferencePolicy(QuestionnaireDraft, Purpose.OPEN_OPERATION, path="choices/preview/staging",
                    extract=questionnaire_selection, conditions=(QuestionnaireDraft.status == "OPEN",),
                    label="Черновик анкеты контрагента", author="author_id", date="uploaded_at",
                    url=lambda row: f"/arm2/questionnaires?draft={row.id}", permissions=("arm2_cp_view",)),
    ReferencePolicy(ApprovalRequest, Purpose.ACTIVE_DOMAIN, path="vehicles_snapshot/response_file_ids",
                    extract=approval_snapshot),
    ReferencePolicy(ApprovalAttempt, Purpose.OPEN_OPERATION, path="plan/target_id", extract=approval_attempt,
                    conditions=(ApprovalAttempt.result.is_(None),), label="Незавершённое согласование",
                    author="principal_id", date="created_at", permissions=("arm2_veh_view", "arm2_document_view"),
                    active=is_active_attempt, retire=lambda row: row.action == "letter",
                    url=lambda row: f"/arm2/approvals/{row.target_id}" if row.target_id.startswith("APPROVAL-") else None),
    ReferencePolicy(FileAcceptance, Purpose.OPEN_OPERATION, path="plan.files/plan.correction", extract=file_acceptance,
                    conditions=(FileAcceptance.state != "accepted",), active=is_active_acceptance,
                    label="Приёмка комплекта", author="principal_id", permissions=("arm2_document_view",)),
    ReferencePolicy(BulkReceipt, Purpose.HISTORY, path="vehicle_id",
                    extract=lambda row: (("VEH", row.vehicle_id, "vehicle_id"),),
                    label="Квитанция импорта техники", author="user_id", date="at",
                    permissions=("arm2_veh_edit",),
                    url=lambda row: f"/arm2/veh/bulk?protocol={row.session_id}&receipt={row.command_id}"),
    ReferencePolicy(QuestionnaireExport, Purpose.HISTORY, path="cp_id",
                    extract=lambda row: (("CP", row.cp_id, "cp_id"),),
                    label="Протокол выгрузки анкеты", author="author_id", date="at",
                    permissions=("arm2_cp_view",), url=lambda row: f"/arm2/questionnaires?export={row.id}"),
    ReferencePolicy(DocumentRevisionContext, Purpose.HISTORY, path="parent_id",
                    extract=lambda row: ((row.parent_type, row.parent_id, "parent_id"),),
                    label="Прежняя привязка редакции документа"),
    ReferencePolicy(QuestionnaireUpload, Purpose.OPEN_OPERATION, path="prepared",
                    extract=questionnaire_upload, author="author_id", date="uploaded_at",
                    label="Подготовленные вложения анкеты", permissions=("arm2_cp_view",),
                    url=lambda draft: f"/arm2/questionnaires?draft={draft.id}",
                    origin=lambda db, upload: db.get(QuestionnaireDraft, upload.draft_id),
                    conditions=(QuestionnaireUpload.draft_id.in_(
                        select(QuestionnaireDraft.id).where(QuestionnaireDraft.status == "OPEN")
                    ),)),
    ReferencePolicy(QuestionnaireReceipt, Purpose.HISTORY, path="result.objects/changes/cp_id",
                    extract=questionnaire_outcome, label="Протокол приёмки анкеты",
                    author="author_id", date="accepted_at", permissions=("arm2_cp_view",),
                    url=lambda row: f"/arm2/questionnaires?draft={row.draft_id}"),
    ReferencePolicy(QuestionnaireReceipt, Purpose.OPEN_OPERATION, path="partial_acceptance",
                    extract=questionnaire_pending_targets, label="Анкета с документами к дозагрузке",
                    author="author_id", date="accepted_at", permissions=("arm2_cp_view",),
                    url=lambda row: f"/arm2/questionnaires?draft={row.draft_id}",
                    conditions=(QuestionnaireReceipt.draft_id.in_(
                        select(QuestionnaireDraft.id).where(
                            QuestionnaireDraft.status == "OPEN",
                            QuestionnaireDraft.payload["_documents_pending"].astext == "true",
                        )
                    ),)),
    ReferencePolicy(ApprovalAttempt, Purpose.HISTORY, path="result/target_id",
                    extract=approval_outcome, conditions=(ApprovalAttempt.result.is_not(None),),
                    label="Завершённая операция согласования", author="principal_id", date="created_at",
                    permissions=("arm2_veh_view", "arm2_document_view")),
    ReferencePolicy(BackgroundOperation, Purpose.OPEN_OPERATION, path="resource_id/payload",
                    extract=questionnaire_job, conditions=(
                        BackgroundOperation.kind == "QUESTIONNAIRE_APPLY",
                        BackgroundOperation.state.in_(("QUEUED", "RUNNING", "FAILED")),
                        BackgroundOperation.resource_id.in_(select(QuestionnaireDraft.id).where(
                            QuestionnaireDraft.status == "OPEN",
                        )),
                    )),
)


# An approval decision and its containing request must be retired together.
INVERSE = ((ApprovalDecision, "request_id", ApprovalRequest),)


def reference_policies():
    physical = {}
    logical = set()
    for policy in (*PHYSICAL, *LOGICAL):
        policy.validate()
        if policy.columns:
            identity = policy.signature()
            if identity in physical:
                raise ObjectRegistryError(f"Повтор внешней ссылки: {identity}")
            physical[identity] = policy
        else:
            identity = (policy.source, policy.path)
            if identity in logical:
                raise ObjectRegistryError(f"Повтор логической ссылки: {policy.path}")
            logical.add(identity)
    return physical


def deletion_policies() -> dict[type, DeletionPolicy]:
    policies = {}
    tables = {}
    for policy in (*OBJECT_REGISTRY.deletion_definitions().values(), *ARTIFACTS.values()):
        policy.validate()
        table = policy.model.__table__
        if table.key in tables:
            raise ObjectRegistryError(f"Конфликт политик удаления для {table.name}")
        tables[table.key] = policy
        policies[policy.model] = policy

    # Immediate foreign keys must follow the registered order unless a
    # nullable internal pointer is cleared. The approval/file cycle uses
    # existing deferred constraints and does not need a preparatory update.
    for policy in policies.values():
        for fk in policy.model.__table__.foreign_key_constraints:
            target = fk.elements[0].target_fullname.rsplit(".", 1)[0]
            if target not in tables:
                continue
            if fk.deferrable and fk.initially == "DEFERRED" and fk.ondelete != "RESTRICT":
                continue
            if any(column.key in policy.clear_fields for column in fk.columns):
                continue
            if policy.order >= tables[target].order:
                raise ObjectRegistryError(f"Некорректный порядок удаления для связи {fk.name or target}")
    return policies


def registered_models(policies: dict[type, DeletionPolicy]):
    reference_policies()
    return {model.__tablename__: model for model in (
        *policies, *(rule.source for rule in (*PHYSICAL, *LOGICAL)), QuestionnaireReceipt, BackgroundOperation,
    )}


def lock_writes(db: Session, models):
    db.execute(text("SET LOCAL lock_timeout = '4s'"))
    # Row locks alone allow new JSON references. Lock only the registered ARM2 relationships
    # while reading the preflight/committing the removal, with no provider calls inside.
    db.execute(text("LOCK TABLE " + ", ".join(sorted(models)) + " IN SHARE ROW EXCLUSIVE MODE"))


def object_key(row):
    kind = OBJECT_REGISTRY.deletion_type_for(type(row))
    if kind is None:
        raise ObjectRegistryError("Не определён тип записи для удаления")
    return kind, row.id


def owner_key(row):
    owners = [(kind, getattr(row, field)) for kind, field in ARTIFACTS[type(row)].owners
              if getattr(row, field) is not None]
    if len(owners) != 1:
        raise ObjectVersionConflictError("Не удалось подтвердить владельца служебной записи")
    return owners[0]


def card_path(kind, row):
    base = OBJECT_REGISTRY.deletion_definition(kind).collection
    if kind == "DOCUMENT":
        return f"{base}/{row.parent_type.lower()}/{row.parent_id}/documents/{row.id}"
    if kind in {"CPREP", "ORGREP"}:
        return f"{base}/{row.parent_id}/representatives/{row.id}"
    if kind == "APPROVAL-ITEM":
        return f"{base}/{row.request_id}#{row.id}"
    return f"{base}/{row.id}"


def artifact_id(row):
    return ":".join(str(getattr(row, field)) for field in ARTIFACTS[type(row)].key_fields)


def artifact_rows(db, selected):
    by_type = {}
    for kind, identifier in map(object_key, selected):
        by_type.setdefault(kind, []).append(identifier)
    for model, policy in ARTIFACTS.items():
        predicates = [getattr(model, field).in_(by_type[kind]) for kind, field in policy.owners if kind in by_type]
        if predicates:
            yield from db.scalars(select(model).where(or_(*predicates), *policy.eligible))


def check_manifests(db, targets):
    revisions = {item.id: item for item in targets if isinstance(item, DocumentRevision)}
    if not revisions:
        return
    contents = {identifier: [] for identifier in revisions}
    for row in db.scalars(select(RevisionFile).where(RevisionFile.revision_id.in_(revisions))
                          .order_by(RevisionFile.revision_id, RevisionFile.position)):
        if row.document_id != revisions[row.revision_id].document_id:
            raise ObjectVersionConflictError("Редакция содержит чужие файлы")
        contents[row.revision_id].append(row.file_id)
    for revision_id, revision in revisions.items():
        if revision.file_ids != contents[revision_id]:
            raise ObjectVersionConflictError(f"Состав редакции {revision_id} не совпадает с манифестом")


def ensure_idle(db, artifacts):
    for row in artifacts:
        policy = ARTIFACTS[type(row)]
        if policy.lease and not db.scalar(text(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,6365))"
        ), {"key": policy.lease(row)}):
            raise ObjectVersionConflictError("Связанная операция ещё выполняется")


def incoming_inverse(db, targets):
    for source, column, container in INVERSE:
        policy = ReferencePolicy(container, Purpose.ACTIVE_DOMAIN, target=source)
        for row in targets:
            if isinstance(row, source):
                related = db.get(container, getattr(row, column))
                if related is None:
                    raise ObjectVersionConflictError("Не удалось подтвердить владельца решения")
                yield IncomingReference(policy, related, row, column)


def incoming_foreign_keys(db, targets, models):
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
    registered = reference_policies()
    inspector = inspect(db.connection())
    for (schema, table), keys in inspector.get_multi_foreign_keys(schema="public").items():
        for fk in keys:
            referenced = fk["referred_table"]
            if referenced not in tables or fk["referred_schema"] not in ("public", None):
                continue
            model = models.get(table)
            if model is None:
                raise ObjectVersionConflictError(f"Нет зарегистрированной политики для связи {schema}.{table}")
            source = model.__table__
            signature = (table, tuple(fk["constrained_columns"]), referenced, tuple(fk["referred_columns"]))
            policy = registered.get(signature)
            known = policy is not None and any(
                tuple(col.name for col in rule.columns) == tuple(fk["constrained_columns"])
                and tuple(item.target_fullname for item in rule.elements)
                == tuple(f"{referenced}.{col}" for col in fk["referred_columns"])
                and (rule.ondelete or "NO ACTION") == fk["options"].get("ondelete", "NO ACTION")
                and bool(rule.deferrable) == bool(fk["options"].get("deferrable"))
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
                yield IncomingReference(policy, row, referenced_rows[key], field)


def target_key(row):
    if isinstance(row, DocumentFile):
        return "FILE", row.id
    if isinstance(row, DocumentRevision):
        return "DOCUMENT-REV", row.id
    if OBJECT_REGISTRY.deletion_type_for(type(row)):
        return object_key(row)
    return None


def incoming_logical(db, targets):
    by_id = {key: row for row in targets if (key := target_key(row))}
    seen = set()
    now = datetime.now(UTC)
    for policy in LOGICAL:
        source = policy.source
        for row in db.scalars(select(source).where(*policy.conditions)):
            try:
                refs = policy.extract(row)
                active = policy.active is None or policy.active(row, now)
                retired = not active and policy.retire is not None and policy.retire(row)
                if not (active or retired):
                    continue
                for kind, identifier, field in refs:
                    if not isinstance(identifier, str) or not isinstance(field, str):
                        raise ValueError("Некорректный формат логической ссылки")
                    if kind in ("FILE", "DOCUMENT-REV"):
                        if not re.fullmatch(f"{kind}-[0-9]{{6,}}", identifier):
                            raise ValueError("Некорректный ID файла или редакции")
                    else:
                        OBJECT_REGISTRY.require(kind).validate_id(identifier)
                    key = (kind, identifier)
                    identity = (policy, id(row), key, field)
                    if key in by_id and identity not in seen:
                        seen.add(identity)
                        yield IncomingReference(policy, row, by_id[key], field, retired)
            except (KeyError, ValueError, TypeError, AttributeError) as error:
                raise ObjectVersionConflictError(
                    f"Неизвестная форма связи {source.__tablename__}.{policy.path}: {error}"
                ) from None
