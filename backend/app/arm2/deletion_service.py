"""The ARM2 deletion transaction. No network or runtime file writes are performed here."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select, text, tuple_, update

from app.arm2 import deletion_relations as relations
from app.arm2.approval_models import ApprovalAttempt, ApprovalDecision, ApprovalRequest
from app.arm2.approval_store import access as approval_access
from app.arm2.bulk_models import BulkSession
from app.arm2.deletion_models import DeletionBatch, DeletionMark
from app.arm2.document_file_models import DocumentFile, FileAcceptance, FileStage
from app.arm2.document_file_storage import STAGE_SECONDS
from app.arm2.document_store import PARENTS, parent_access
from app.arm2.file_read_guard import ensure_no_download
from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import append_history_event
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectRegistryError
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError
from app.arm2.operation_models import BackgroundOperation
from app.arm2.questionnaire_models import QuestionnaireDraft
from app.models import User
from app.permissions import Permission, has_permission

CLEANUP = "DELETION_CLEANUP"
PREVIEW_TIMEOUT = timedelta(minutes=20)


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


def required_permissions(kind, saved_scope, *, edit=False):
    try:
        OBJECT_REGISTRY.deletion_definition(kind)
        edit_permission = OBJECT_REGISTRY.require(kind).edit_permission
        needed = {Permission(edit_permission.value.removesuffix("_edit") + "_view")}
        if edit:
            needed.add(edit_permission)
        if kind == "DOCUMENT" or kind in {"CPREP", "ORGREP"}:
            if not isinstance(saved_scope, dict) or set(saved_scope) != {"parent_type", "parent_id"}:
                raise ValueError("Invalid parent scope")
            parent_type, parent_id = saved_scope["parent_type"], saved_scope["parent_id"]
            if kind != "DOCUMENT" and parent_type != kind.removesuffix("REP"):
                raise ValueError("Invalid representative scope")
            OBJECT_REGISTRY.require(parent_type).validate_id(parent_id)
            if kind == "DOCUMENT":
                _, view_parent, edit_parent = PARENTS[parent_type]
                needed.add(view_parent)
                if edit:
                    needed.add(edit_parent)
        elif kind in {"APPROVAL", "APPROVAL-ITEM"}:
            if saved_scope != {"type": "VEH"}:
                raise ValueError("Invalid approval scope")
            needed.add(Permission.ARM2_DOCUMENT_VIEW)
            if edit:
                needed.add(Permission.ARM2_DOCUMENT_EDIT)
        elif saved_scope != {}:
            raise ValueError("Unexpected object scope")
        return needed
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ObjectAuthorizationError("Область доступа объекта удаления не подтверждена") from None


def object_access(db, user, kind, row, *, edit=False):
    needed = required_permissions(kind, scope(kind, row), edit=edit)
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
                    "name": f"{relations.ARTIFACTS[model].name} · {artifact}",
                    "reason": reason, "blocking": reason != "Принадлежит выбранному объекту"}
        row = lookup(db, *key)
    if OBJECT_REGISTRY.deletion_type_for(type(row)) is not None:
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


def preflight(db, user, keys, models):
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
    sources = [*relations.incoming_foreign_keys(db, targets, models), *relations.incoming_logical(db, targets)]
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
    models = relations.registered_models(relations.deletion_policies())
    relations.lock_writes(db, models)
    snapshot, _, _ = preflight(db, user, keys, models)
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


def _removal_plan(rows, artifacts, policies):
    keys_by_model = {}
    for row in (*rows, *artifacts):
        model = type(row)
        if model not in policies:
            raise ObjectRegistryError("Не определена политика удаления записи")
        key = tuple(getattr(row, col.key) for col in model.__table__.primary_key)
        keys = keys_by_model.setdefault(model, set())
        if None in key or key in keys:
            raise ObjectRegistryError("Некорректный ключ записи в наборе удаления")
        keys.add(key)
    ordered = sorted(keys_by_model, key=lambda model: (policies[model].order, model.__table__.name))
    return [(policies[model], keys_by_model[model]) for model in ordered]


def _key_filter(columns, keys):
    if len(columns) == 1:
        return columns[0].in_([key[0] for key in keys])
    return tuple_(*columns).in_(list(keys))


def _remove(db, plan):
    # Clear only declared fields on rows selected for this same transaction.
    for policy, keys in plan:
        if policy.clear_fields:
            table = policy.model.__table__
            db.execute(update(table).where(_key_filter(list(table.primary_key), keys))
                       .values({field: None for field in policy.clear_fields}))
    for policy, keys in plan:
        table = policy.model.__table__
        columns = list(table.primary_key)
        removed = db.execute(delete(table).where(_key_filter(columns, keys)).returning(*columns))
        if {tuple(key) for key in removed} != keys:
            raise ObjectVersionConflictError("База не подтвердила удаление всех записей набора")
    # Check deferred constraints and verify final absence after all DELETEs.
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for policy, keys in plan:
        columns = list(policy.model.__table__.primary_key)
        if db.execute(select(*columns).where(_key_filter(columns, keys)).limit(1)).first() is not None:
            raise ObjectVersionConflictError("После удаления в базе остались записи набора")


def execute(db, user, batch_id):
    user = authorize(db, user, execute=True, lock=True)
    policies = relations.deletion_policies()
    models = relations.registered_models(policies)
    ensure_no_download(db)
    relations.lock_writes(db, models)
    batch = stored_batch(db, user, batch_id, lock=True)
    if batch.executed_at:
        raise ObjectVersionConflictError("Набор уже удалён. Откройте протокол удаления")
    if datetime.now(UTC) - batch.created_at > PREVIEW_TIMEOUT:
        raise ObjectVersionConflictError("Срок проверки истёк. Проверьте набор заново")
    keys = [(entry["object_type"], entry["object_id"]) for entry in batch.snapshot["objects"].values()]
    new, rows, artifacts = preflight(db, user, keys, models)
    if new != batch.snapshot:
        raise ObjectVersionConflictError("Версии, пометки или связи изменились. Проверьте набор заново")
    if any(link["blocking"] for link in new["dependencies"]):
        raise ObjectVersionConflictError("Есть блокирующие связи. Исправьте их и проверьте набор заново")
    plan = _removal_plan(rows, artifacts, policies)
    audit = {}
    for row in artifacts:
        audit.setdefault(row.__tablename__, {})[relations.artifact_id(row)] = _audit_row(row)
    batch.artifacts = audit
    batch.executed_at = datetime.now(UTC)
    batch.execution_tx = db.scalar(text("SELECT txid_current()::text"))
    db.flush()  # The immutable-artifact guard reads this batch in the same transaction.
    _remove(db, plan)
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
    db.flush()
    return receipt(db, batch, user)


def batch_access_allowed(batch, user, *, edit=False):
    try:
        objects = batch.snapshot["objects"]
        if not isinstance(objects, dict) or not objects:
            return False
        return all(has_permission(user, permission) for item in objects.values()
                   for permission in required_permissions(item["object_type"], item["scope"], edit=edit))
    except (KeyError, TypeError, ObjectAuthorizationError):
        return False


def receipt(db, batch, user):
    if not batch_access_allowed(batch, user):
        return {"id": batch.id, "state": "RESTRICTED", "executed_at": batch.executed_at,
                "actor_id": batch.actor_id, "objects": [], "cleanup": [], "can_retry": False}
    jobs = db.scalars(select(BackgroundOperation).where(
        BackgroundOperation.kind == CLEANUP, BackgroundOperation.payload["batch_id"].astext == batch.id
    ).order_by(BackgroundOperation.id)).all()
    state = "CHECKED" if not batch.executed_at else "COMPLETED"
    if any(row.state != "SUCCEEDED" for row in jobs):
        state = "FILES_BLOCKED" if any(row.state == "FAILED" for row in jobs) else "FILES_PENDING"
    can_retry = (batch.executed_at is not None and state == "FILES_BLOCKED"
                 and has_permission(user, Permission.ARM2_DELETE_EXECUTE)
                 and batch_access_allowed(batch, user, edit=True))
    return {"id": batch.id, "state": state, "executed_at": batch.executed_at,
            "actor_id": batch.actor_id, "objects": list(batch.snapshot["objects"].values()), "can_retry": can_retry,
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
