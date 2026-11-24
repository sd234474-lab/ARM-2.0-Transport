"""The ARM2 deletion transaction. No network or runtime file writes are performed here."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select, text, update

from app.arm2 import deletion_relations as relations
from app.arm2.approval_models import ApprovalAttempt, ApprovalDecision, ApprovalRequest
from app.arm2.approval_store import access as approval_access
from app.arm2.bulk_models import BulkSession
from app.arm2.deletion_models import DeletionBatch, DeletionMark
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
from app.arm2.document_models import Document
from app.arm2.document_store import PARENTS, parent_access
from app.arm2.file_read_guard import ensure_no_download
from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import append_history_event
from app.arm2.object_registry import OBJECT_REGISTRY
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError
from app.arm2.operation_models import BackgroundOperation
from app.arm2.questionnaire_models import QuestionnaireDraft
from app.arm2.vehicle_models import Vehicle
from app.models import User
from app.permissions import Permission, has_permission

CLEANUP = "DELETION_CLEANUP"
PREVIEW_TIMEOUT = timedelta(minutes=20)
WRITE_ORDER = (
    ApprovalDecision, DocumentFileCorrection, RevisionFile, DocumentRevisionContext,
    FileAcceptance, FileStage, DocumentRevision, DocumentFile, ApprovalRequest, Document,
    Vehicle, relations.OBJECTS["CPREP"].model, relations.OBJECTS["ORGREP"].model,
    relations.OBJECTS["CP"].model, relations.OBJECTS["ORG"].model,
    relations.OBJECTS["DOC-KIND"].model, relations.OBJECTS["TYPE"].model,
)


def identity(kind, identifier):
    OBJECT_REGISTRY.require(kind).validate_id(identifier)
    return kind, identifier


def lookup(db, kind, identifier, *, lock=False):
    identity(kind, identifier)
    model = OBJECT_REGISTRY.deletion_definition(kind).model
    query = select(model).where(model.id == identifier).execution_options(populate_existing=True)
    record = db.scalar(query.with_for_update() if lock else query)
    if record is None:
        raise ObjectNotFoundError("Объект АРМ 2.0 не найден")
    return record


def authorize(db, user, *, execute=False, lock=False):
    query = select(User).where(User.id == user.id).execution_options(populate_existing=True)
    current = db.scalar(query.with_for_update(read=True) if lock else query)
    permission = Permission.ARM2_DELETE_EXECUTE if execute else Permission.ARM2_DELETE_MARK
    if not current or not current.active or current.deleted_at is not None or not has_permission(current, permission):
        raise ObjectAuthorizationError("Недостаточно прав для операции удаления")
    return current


def scope(kind, row):
    if kind == "DOCUMENT":
        return {"parent_type": row.parent_type, "parent_id": row.parent_id}
    if kind in {"CPREP", "ORGREP"}:
        return {"parent_type": kind.removesuffix("REP"), "parent_id": row.parent_id}
    if kind in {"APPROVAL", "APPROVAL-ITEM"}:
        return {"type": "VEH"}
    return {}


def required_permissions(kind, saved_scope):
    edit = OBJECT_REGISTRY.require(kind).edit_permission
    needed = {Permission(edit.value.replace("_edit", "_view"))}
    if kind in {"APPROVAL", "APPROVAL-ITEM"}:
        needed.add(Permission.ARM2_DOCUMENT_VIEW)
    if kind == "DOCUMENT":
        needed.add(PARENTS[saved_scope["parent_type"]][1])
    return needed


def object_access(db, user, kind, row, *, edit=False):
    needed = required_permissions(kind, scope(kind, row))
    if edit:
        needed.add(OBJECT_REGISTRY.require(kind).edit_permission)
        if kind in {"APPROVAL", "APPROVAL-ITEM"}:
            needed.add(Permission.ARM2_DOCUMENT_EDIT)
    if any(not has_permission(user, item) for item in needed):
        raise ObjectAuthorizationError("Нет прав на объект удаления")
    if kind == "DOCUMENT":
        parent_access(db, user, row.parent_type, row.parent_id, edit=edit)
    if kind in {"APPROVAL", "APPROVAL-ITEM"}:
        approval_access(db, user, row.request_id if kind == "APPROVAL-ITEM" else row.id, edit=edit)


def public_object(kind, row, mark):
    return {
        "object_type": kind, "object_id": row.id, "version": row.version,
        "name": getattr(row, "name", row.id), "path": relations.card_path(kind, row),
        "scope": scope(kind, row), "mark": mark.command_id if mark else None,
    }


def get_mark(db, user, key):
    user = authorize(db, user)
    row = lookup(db, *key)
    object_access(db, user, key[0], row, edit=True)
    return public_object(key[0], row, db.get(DeletionMark, key))


def write_mark(db, user, key, version, expected_mark, marked):
    user = authorize(db, user, lock=True)
    row = lookup(db, *key, lock=True)
    object_access(db, user, key[0], row, edit=True)
    prior = db.get(DeletionMark, key, populate_existing=True)
    if row.version != version or (prior.command_id if prior else None) != expected_mark:
        raise ObjectVersionConflictError("Карточка или пометка изменилась. Откройте актуальное состояние")
    if marked == bool(prior):
        return public_object(key[0], row, prior)
    command = str(uuid4())
    mark = DeletionMark(object_type=key[0], object_id=key[1], actor_id=user.id, command_id=command) if marked else None
    if mark:
        db.add(mark)
    else:
        db.delete(prior)
    append_history_event(
        db, object_type=key[0], object_id=key[1], operation="MARKED" if marked else "UNMARKED",
        actor_kind="HUMAN", actor_id=str(user.id), source="arm2-api-deletion", correlation_id=command,
        version_before=row.version, version_after=row.version,
        changes={"deletion_mark": {"before": not marked, "after": marked}},
    )
    return public_object(key[0], row, mark)


def marked_objects(db, user, offset, limit):
    user = authorize(db, user)
    result = []
    marks = db.scalars(select(DeletionMark).order_by(
        DeletionMark.occurred_at.desc(), DeletionMark.object_type, DeletionMark.object_id))
    for mark in marks:
        try:
            row = lookup(db, mark.object_type, mark.object_id)
            object_access(db, user, mark.object_type, row, edit=True)
        except (ObjectAuthorizationError, ObjectNotFoundError):
            continue
        result.append(public_object(mark.object_type, row, mark))
        if len(result) == offset + limit + 1:
            break
    return {"items": result[offset:offset + limit], "has_more": len(result) > offset + limit}


def active(row):
    now = datetime.now(UTC)
    if isinstance(row, FileStage):
        return row.expires_at > now and row.state != "consumed"
    if isinstance(row, FileAcceptance):
        return row.expires_at > now and row.state != "accepted"
    return row.created_at + timedelta(seconds=STAGE_SECONDS) > now


def dependency(db, user, row, target, field, selected):
    model = type(row)
    target_key = relations.owner_key(target) if type(target) in relations.ARTIFACTS else relations.object_key(target)
    # Scope reports hide objects outside the operator's current rights, while retaining a blocker.
    entry = {"target_type": target_key[0], "target_id": target_key[1], "field": field}
    if model in relations.ARTIFACTS:
        key = relations.owner_key(row)
        if key in selected:
            reason = "Принадлежит выбранному объекту"
            if isinstance(row, (FileStage, FileAcceptance)) and active(row):
                reason = "Идёт работа с файлами. Завершите её или дождитесь окончания срока черновика"
            artifact = relations.artifact_id(row)
            return {**entry, "object_type": row.__tablename__, "object_id": artifact,
                    "name": f"{relations.ARTIFACT_NAMES[model]} · {artifact}",
                    "reason": reason, "blocking": reason != "Принадлежит выбранному объекту"}
        row = lookup(db, *key)
    if row.__tablename__ in relations.SOURCE_TYPES:
        kind, identifier = relations.object_key(row)
        try:
            object_access(db, user, kind, row)
        except ObjectAuthorizationError:
            return {**entry, "blocking": True, "reason": "Связанный объект вне ваших прав доступа"}
        return {
            **entry, "object_type": kind, "object_id": identifier, "name": getattr(row, "name", identifier),
            "path": relations.card_path(kind, row), "blocking": (kind, identifier) not in selected,
            "reason": "Выбран вместе с этой записью" if (kind, identifier) in selected
            else "Исправьте связь в карточке или явно добавьте объект в набор",
        }
    if model in relations.HISTORICAL_FK:
        label, id_field, _ = relations.HISTORICAL_FK[model]
        return {**entry, "object_type": row.__tablename__, "object_id": getattr(row, id_field)
                if (getattr(row, "author_id", None) or getattr(row, "user_id", None)) == user.id else None,
                "blocking": True, "reason": f"{label}. Связь защищена как часть истории"}
    if isinstance(row, (ApprovalAttempt, BulkSession, QuestionnaireDraft)):
        owned = (getattr(row, "principal_id", None) or getattr(row, "author_id", None)
                 or getattr(row, "user_id", None)) == user.id
        path = "/arm2/questionnaires" if isinstance(row, QuestionnaireDraft) else "/arm2/veh/bulk"
        if isinstance(row, ApprovalAttempt):
            path = "/arm2/approvals"
        return {**entry, "object_type": row.__tablename__, "object_id": row.id if owned else None,
                "path": path if owned else None, "blocking": True,
                "reason": "Незавершённая операция. Завершите или отмените её"}
    raise ObjectVersionConflictError("Неизвестная связь объекта. Удаление заблокировано")


def preflight(db, user, keys):
    keys = sorted(keys)
    if len(keys) != len(set(keys)):
        raise ValueError("В наборе повторяются объекты")
    user = authorize(db, user, lock=True)
    rows = []
    objects = {}
    for key in keys:
        kind, identifier = key
        row = lookup(db, *key, lock=True)
        object_access(db, user, kind, row, edit=True)
        mark = db.get(DeletionMark, key, populate_existing=True)
        if not mark:
            raise ObjectVersionConflictError(f"Сначала пометьте {identifier} на удаление")
        rows.append(row)
        objects[identifier] = public_object(kind, row, mark)

    artifacts = list(relations.artifact_rows(db, rows))
    targets = [*rows, *artifacts]
    sources = [*relations.incoming_foreign_keys(db, targets), *relations.incoming_logical(db, targets)]
    for row in rows:
        if isinstance(row, ApprovalDecision):
            sources.append((lookup(db, "APPROVAL", row.request_id), row, "request_id"))
    for row in db.scalars(select(ApprovalAttempt).where(
        ApprovalAttempt.action == "response-stage", ApprovalAttempt.target_id.in_(
            [item.id for item in rows if isinstance(item, ApprovalRequest)]))):
        request = next(item for item in rows if item.id == row.target_id)
        if row.result is None and active(row) and not db.get(DocumentFile, row.plan["file"]["id"]):
            sources.append((row, request, "target_id"))
        else:
            artifacts.append(row)
    dependent = {}
    selection = set(keys)
    for source, target, field in sources:
        link = dependency(db, user, source, target, field, selection)
        identity = (link.get("target_id"), link.get("object_type"), link.get("object_id"), field, link["reason"])
        dependent[identity] = link
    return {"objects": objects, "dependencies": sorted(dependent.values(), key=lambda x: (
        x["target_id"], x.get("object_type") or "", x.get("object_id") or "", x["field"], x["reason"]
    ))}, rows, artifacts


def preview(db, user, keys):
    relations.lock_writes(db)
    snapshot, _, _ = preflight(db, user, keys)
    batch = DeletionBatch(id=str(uuid4()), actor_id=user.id, snapshot=snapshot)
    db.add(batch)
    return {"id": batch.id, **snapshot, "can_execute": has_permission(user, Permission.ARM2_DELETE_EXECUTE)
            and not any(link["blocking"] for link in snapshot["dependencies"])}


def stored_batch(db, user, batch_id, *, lock=False):
    stmt = select(DeletionBatch).where(DeletionBatch.id == batch_id, DeletionBatch.actor_id == user.id)
    batch = db.scalar(stmt.with_for_update() if lock else stmt)
    if batch is None:
        raise ObjectNotFoundError("Проверка набора удаления не найдена")
    return batch


def _audit_row(row):
    # Do not retain session credentials or transient plans in the historical artifact image.
    omitted = {"plan", "session_hash", "principal_id"}
    return jsonable_encoder({col.key: getattr(row, col.key) for col in row.__table__.columns if col.key not in omitted})


def _cleanup_jobs(db, user, batch, artifacts):
    originals = {}
    locals_ = {}
    for row in artifacts:
        if isinstance(row, DocumentFile):
            originals[row.storage_ref] = {key: getattr(row, key) for key in ("id", "storage_ref", "sha256", "size")}
        if isinstance(row, FileAcceptance):
            for item in row.plan["files"]:
                if not item["existing"]:
                    originals[item["storage_ref"]] = {key: item[key] for key in ("id", "storage_ref", "sha256", "size")}
        if isinstance(row, ApprovalAttempt):
            item = row.plan["file"]
            locals_[item["token"]] = item["id"]
            originals[item["storage_ref"]] = {key: item[key] for key in ("id", "storage_ref", "sha256", "size")}
        if isinstance(row, FileStage):
            locals_[row.id] = row.file_id
    for payload in [*originals.values(), *({"token": key, "id": value} for key, value in locals_.items())]:
        job_id = str(uuid4())
        db.add(BackgroundOperation(
            id=job_id, kind=CLEANUP, resource_id=job_id, author_id=user.id, state="QUEUED",
            payload={**payload, "batch_id": batch.id}, progress={"phase": "QUEUED"},
        ))


def _remove(db, rows, artifacts):
    # Break only internal RESTRICT cycles for records that will be removed in this transaction.
    # Approval letter/package references are already deferred by schema 0072.
    for row in rows:
        if isinstance(row, Document):
            db.execute(update(Document).where(Document.id == row.id).values(current_revision_id=None))
        if isinstance(row, Vehicle) and row.calibration_document_id:
            db.execute(update(Vehicle).where(Vehicle.id == row.id).values(
                calibration_m3=None, calibration_confirmed_by=None, calibration_confirmed_at=None,
                calibration_document_id=None, calibration_revision_id=None, calibration_plate_number=None,
            ))
        if isinstance(row, (relations.OBJECTS["CP"].model, relations.OBJECTS["ORG"].model)):
            model = type(row)
            db.execute(update(model).where(model.id == row.id).values(primary_signer_id=None))
    for model in (*WRITE_ORDER, ApprovalAttempt):
        for row in [item for item in (*rows, *artifacts) if type(item) is model]:
            table = model.__table__
            db.execute(delete(table).where(*(col == getattr(row, col.key) for col in table.primary_key.columns)))


def execute(db, user, batch_id):
    user = authorize(db, user, execute=True, lock=True)
    ensure_no_download(db)
    relations.lock_writes(db)
    batch = stored_batch(db, user, batch_id, lock=True)
    if batch.executed_at:
        raise ObjectVersionConflictError("Набор уже удалён. Откройте протокол удаления")
    if datetime.now(UTC) - batch.created_at > PREVIEW_TIMEOUT:
        raise ObjectVersionConflictError("Срок проверки истёк. Проверьте набор заново")
    keys = [(entry["object_type"], entry["object_id"]) for entry in batch.snapshot["objects"].values()]
    new, rows, artifacts = preflight(db, user, keys)
    if new != batch.snapshot:
        raise ObjectVersionConflictError("Версии, пометки или связи изменились. Проверьте набор заново")
    if any(link["blocking"] for link in new["dependencies"]):
        raise ObjectVersionConflictError("Есть блокирующие связи. Исправьте их и проверьте набор заново")
    audit = {}
    for row in artifacts:
        audit.setdefault(row.__tablename__, {})[relations.artifact_id(row)] = _audit_row(row)
    batch.artifacts = audit
    batch.executed_at = datetime.now(UTC)
    batch.execution_tx = db.scalar(text("SELECT txid_current()::text"))
    db.flush()
    _cleanup_jobs(db, user, batch, artifacts)
    for key in keys:
        mark = db.get(DeletionMark, key)
        version = new["objects"][key[1]]["version"]
        append_history_event(
            db, object_type=key[0], object_id=key[1], operation="DELETED",
            actor_kind="HUMAN", actor_id=str(user.id), source="arm2-api-deletion", correlation_id=batch.id,
            version_before=version, changes={"version": {"before": version, "after": None}},
        )
        db.delete(mark)
    _remove(db, rows, artifacts)
    db.flush()
    return receipt(db, batch, user)


def receipt(db, batch, user):
    objects = list(batch.snapshot["objects"].values())
    if any(not has_permission(user, permission) for item in objects
           for permission in required_permissions(item["object_type"], item["scope"])):
        return {"id": batch.id, "state": "RESTRICTED", "executed_at": batch.executed_at,
                "actor_id": batch.actor_id, "objects": [], "cleanup": []}
    jobs = db.scalars(select(BackgroundOperation).where(
        BackgroundOperation.kind == CLEANUP, BackgroundOperation.payload["batch_id"].astext == batch.id
    ).order_by(BackgroundOperation.id)).all()
    state = "CHECKED" if not batch.executed_at else "COMPLETED"
    if any(row.state != "SUCCEEDED" for row in jobs):
        state = "FILES_BLOCKED" if any(row.state == "FAILED" for row in jobs) else "FILES_PENDING"
    return {"id": batch.id, "state": state, "executed_at": batch.executed_at,
            "actor_id": batch.actor_id, "objects": objects,
            "cleanup": [{"object_id": row.payload["id"], "state": row.state, "failure": row.failure} for row in jobs]}


def deletion_history_allowed(db, user, kind, identifier):
    batch_id = db.scalar(select(HistoryEvent.correlation_id).where(
        HistoryEvent.object_type == kind, HistoryEvent.object_id == identifier, HistoryEvent.operation == "DELETED"
    ))
    if not batch_id:
        return False
    batch = db.get(DeletionBatch, batch_id)
    if not batch or not batch.executed_at:
        return False
    saved = batch.snapshot["objects"][identifier]["scope"]
    if any(not has_permission(user, code) for code in required_permissions(kind, saved)):
        raise ObjectAuthorizationError("Нет прав на историю удалённого объекта")
    return True
