"""Two-phase durable upload -> atomic common SAVE; failed attempts remain retryable."""

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.arm2 import document_file_storage as files
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
from app.arm2.document_store import ADAPTER, PARENTS, parent_access
from app.arm2.file_read_guard import try_exclusive_file_access
from app.arm2.ids import next_id
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
    update_object,
)
from app.models import LoginSession


def instant():
    return datetime.now(UTC)


def document_access(db, user, parent_type, parent_id, document_id, *, edit=False):
    parent_access(db, user, parent_type, parent_id, edit=edit)
    record = db.scalar(
        select(Document)
        .where(
            Document.id == document_id, Document.parent_type == parent_type, Document.parent_id == parent_id
        )
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise ObjectNotFoundError("Документ этой карточки не найден")
    return record


def owned_stage(db, user, session_hash, document_id, stage_id, *, ready=True, valid_at=None):
    stage = db.get(FileStage, stage_id, populate_existing=True)
    if (
        stage is None
        or stage.document_id != document_id
        or stage.principal_id != user.id
        or stage.session_hash != session_hash
    ):
        raise ObjectAuthorizationError("Нет доступа к временному файлу")
    if stage.expires_at <= (valid_at or instant()) or stage.state == "consumed":
        raise ValueError("Срок действия временного файла истёк; выберите файл снова")
    if ready and stage.state != "ready":
        raise ValueError("Временный файл ещё не загружен")
    return stage


def revision_files(db, document_id, revision_id):
    return (
        list(
            db.scalars(
                select(DocumentFile)
                .join(RevisionFile, RevisionFile.file_id == DocumentFile.id)
                .where(RevisionFile.document_id == document_id, RevisionFile.revision_id == revision_id)
                .order_by(RevisionFile.position)
            )
        )
        if revision_id
        else []
    )


def display_state(db, document, *, current_files=None):
    if document.archive_state == "Архивный":
        return "Архивный"
    current = current_files if current_files is not None else revision_files(db, document.id, document.current_revision_id)
    if not current:
        return "Не загружен"
    # No stale positive cache: unknown, timeout and missing/corrupt bytes fail closed.
    deadline = db.info.setdefault("document_availability_deadline", time.monotonic() + 2)
    return (
        "Доступен"
        if all(time.monotonic() < deadline and files.verify_remote(row) for row in current)
        else "Файл недоступен"
    )


def export_name(db, document, original, *, revision_id=files.CURRENT_REVISION):
    """Current external name only; never modifies the accepted FILE or its physical path."""
    kind = db.get(DocumentKind, document.kind_ref, populate_existing=True)
    parent = db.get(PARENTS[document.parent_type][0], document.parent_id, populate_existing=True)
    return files.safe_name(kind.file_naming_template, document, parent, kind, original.id, original.extension,
                           revision_id=revision_id)


def public_file(row, position, correction=None, *, download_name=None):
    return {
        key: getattr(row, key)
        for key in ["id", "original_upload_name", "stored_name", "size", "sha256", "media_type", "extension"]
    } | ({"download_name": download_name} if download_name is not None else {}) | {"position": position, "correction": ({
        "reason_code": correction.reason_code, "operation": correction.operation,
        "actor_id": correction.actor_id, "occurred_at": correction.occurred_at.isoformat(),
        "history_event_id": correction.history_event_id,
    } if correction else None)}


def bundles(db, document, include_history=True, *, user):
    from app.arm2.deletion_service import resolve_reference

    revisions = []
    for revision in db.scalars(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == document.id,
               True if include_history else DocumentRevision.id == document.current_revision_id)
        .order_by(DocumentRevision.number.desc())
    ):
        snapshot = db.get(DocumentRevisionContext, revision.id)
        if snapshot is None:
            raise ValueError("Историческая привязка редакции не найдена")
        parent = resolve_reference(db, user, snapshot.parent_type, snapshot.parent_id)
        parent.pop("version", None)  # The revision retains its own historical version.
        revisions.append(
            {
                "id": revision.id,
                "number": revision.number,
                "document_id": document.id,
                "parent_type": snapshot.parent_type,
                "parent_id": snapshot.parent_id if parent["state"] != "RESTRICTED" else None,
                "parent_current_object": parent,
                "author_id": revision.author_id,
                "occurred_at": revision.occurred_at.isoformat(),
                "source": revision.source,
                "files": [
                    public_file(row, i, db.get(DocumentFileCorrection, row.id),
                                download_name=export_name(db, document, row, revision_id=revision.id))
                    for i, row in enumerate(revision_files(db, document.id, revision.id))
                ],
            }
        )
    return {
        "document_id": document.id,
        "document_version": document.version,
        "current_revision_id": document.current_revision_id,
        "display_state": display_state(db, document),
        "revisions": revisions,
        "max_files": files.MAX_FILES,
        "max_file_bytes": files.settings().max_file_bytes,
    }


@contextmanager
def attempt_lease(db, key):
    # Session advisory lock spans network IO, not a business/row transaction.
    with db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        acquired = lock.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key,6365))"), {"key": key})
        if not acquired:
            raise ObjectVersionConflictError("Запрос уже выполняется; повторите с тем же ключом")
        try:
            yield
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,6365))"), {"key": key})


def reauthorize(db, user, session_hash, ptype, pid, docid):
    session = db.get(LoginSession, session_hash, populate_existing=True)
    if session is None or session.expires_at <= instant():
        raise ObjectAuthorizationError("Сессия завершена; принятие не выполнено")
    return document_access(db, user, ptype, pid, docid, edit=True)


def acceptance_fingerprint(document_id, expected_version, selection, correction=None):
    request = {"document": document_id, "expected_version": expected_version, "files": selection}
    if correction is not None:
        request["correction"] = correction
    return hashlib.sha256(
        json.dumps(
            request,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def prepare(db, user, session_hash, document, expected_version, key, selection, correction=None):
    fingerprint = acceptance_fingerprint(document.id, expected_version, selection, correction)
    attempt = db.get(FileAcceptance, key, populate_existing=True)
    if attempt:
        if (attempt.document_id, attempt.principal_id, attempt.session_hash) != (
            document.id,
            user.id,
            session_hash,
        ):
            raise ObjectAuthorizationError("Ключ принятия принадлежит другому запросу")
        if attempt.fingerprint != fingerprint:
            raise ObjectVersionConflictError("Ключ уже связан с другим составом")
        if attempt.state != "accepted" and attempt.expires_at <= instant():
            raise ValueError("Запрос истёк; выберите файлы снова")
        return attempt
    if document.version != expected_version:
        raise ObjectVersionConflictError("Документ изменён другим оператором")
    current = revision_files(db, document.id, document.current_revision_id)
    if correction:
        if correction["file_id"] not in {row.id for row in current}:
            raise ValueError("Исправить можно только файл текущего комплекта")
        if db.get(DocumentFileCorrection, correction["file_id"]):
            raise ValueError("Ошибочная загрузка уже исправлена")
        selection = []
        for row in current:
            if row.id != correction["file_id"]:
                selection.append({"file_id": row.id})
            elif correction.get("stage_id"):
                selection.append({"stage_id": correction["stage_id"]})
    current_ids = {row.id for row in current}
    existing = {
        row.sha256: row
        for row in db.scalars(select(DocumentFile).where(DocumentFile.document_id == document.id))
    }
    resolved = []
    kind = db.get(DocumentKind, document.kind_ref)
    parent = db.get(PARENTS[document.parent_type][0], document.parent_id)
    revision_id = next_id(db, "DOCUMENT-REV") if selection or current else None
    seen = set()
    for item in selection:
        stage = None
        if "stage_id" in item:
            stage = owned_stage(db, user, session_hash, document.id, item["stage_id"])
            files.verified_stage(stage)
            row = existing.get(stage.sha256)
        else:
            row = db.get(DocumentFile, item["file_id"])
            if row is None or row.document_id != document.id or row.id not in current_ids:
                raise ObjectAuthorizationError("Файл не принадлежит текущему составу документа")
        source = row or stage
        if row is not None and db.get(DocumentFileCorrection, row.id):
            raise ValueError("Исправленная ошибочная загрузка не может включаться в рабочий комплект")
        if correction and row is not None and row.id == correction["file_id"]:
            raise ValueError("Верный оригинал должен отличаться от ошибочно загруженного файла")
        if source.sha256 in seen:
            raise ValueError("Одинаковые файлы нельзя дважды включать в один комплект")
        seen.add(source.sha256)
        if row:
            data = {
                name: getattr(row, name)
                for name in [
                    "id",
                    "original_upload_name",
                    "stored_name",
                    "storage_ref",
                    "size",
                    "sha256",
                    "media_type",
                    "extension",
                ]
            }
        else:
            name = files.safe_name(
                kind.file_naming_template, document, parent, kind, stage.file_id, stage.extension,
                revision_id=revision_id
            )
            target = "/".join(
                [
                    files.document_root(),
                    document.parent_type,
                    document.parent_id,
                    document.id,
                    revision_id,
                    stage.file_id,
                    name,
                ]
            )
            data = {
                "id": stage.file_id,
                "original_upload_name": stage.original_upload_name,
                "stored_name": name,
                "storage_ref": target,
                "size": stage.size,
                "sha256": stage.sha256,
                "media_type": stage.media_type,
                "extension": stage.extension,
            }
        resolved.append({**data, "stage_id": stage.id if stage else None, "existing": row is not None})
    noop = [row["id"] for row in resolved] == [row.id for row in current]
    previous = (
        db.get(DocumentRevision, document.current_revision_id) if document.current_revision_id else None
    )
    plan = {
        "revision_id": document.current_revision_id if noop else revision_id,
        "number": (previous.number + 1) if previous else 1,
        "noop": noop,
        "files": resolved,
        "uploaded": [],
        "correction": correction,
    }
    attempt = FileAcceptance(
        id=key,
        document_id=document.id,
        principal_id=user.id,
        session_hash=session_hash,
        expected_version=expected_version,
        fingerprint=fingerprint,
        plan=plan,
        state="prepared",
        result=None,
        expires_at=instant() + timedelta(seconds=files.STAGE_SECONDS),
    )
    db.add(attempt)
    db.commit()  # Durable private retry ledger, never a published DOCUMENT revision.
    return attempt


def accept(db, user, session_hash, ptype, pid, docid, expected_version, key, selection, correction=None):
    from app.arm2.deletion_service import deleted_reference, resolve_reference, retired_command

    with attempt_lease(db, key):
        started_at = instant()
        retired = retired_command(db, FileAcceptance, key)
        if retired is not None:
            if retired.get("principal_id") != user.id or retired.get("document_id") != docid:
                raise ObjectAuthorizationError("Ключ принятия принадлежит другому запросу")
            if retired["fingerprint"] != acceptance_fingerprint(docid, expected_version, selection, correction):
                raise ObjectVersionConflictError("Ключ уже связан с другим составом")
            current = resolve_reference(db, user, "DOCUMENT", docid, edit=True)
            if current["state"] == "RESTRICTED":
                raise ObjectAuthorizationError("Нет доступа к протоколу операции")
            if current["state"] != "DELETED" or not retired.get("result"):
                raise ObjectVersionConflictError("Команда уже закрыта. Откройте протокол удаления")
            _, snapshot = deleted_reference(db, "DOCUMENT", docid)
            if snapshot["scope"] != {"parent_type": ptype, "parent_id": pid}:
                raise ObjectAuthorizationError("Документ не принадлежит указанному родителю")
            return {**retired["result"], "current_object": current}
        document = reauthorize(db, user, session_hash, ptype, pid, docid)
        attempt = prepare(db, user, session_hash, document, expected_version, key, selection, correction)
        if attempt.state == "accepted":
            return attempt.result
        plan = attempt.plan
        if plan["files"]:
            root = files.document_root() + "/"
            if any(not item["storage_ref"].startswith(root) for item in plan["files"]):
                raise files.StorageUnavailable(
                    "Оригинал вне настроенного защищённого корня; запись запрещена"
                )
        db.commit()  # Release read transactions before potentially slow provider operations.
        for item in plan["files"]:
            reauthorize(db, user, session_hash, ptype, pid, docid)
            db.commit()
            provider = files.storage()
            if not files.verify_remote(type("FileMetadata", (), item), provider):
                if not item["stage_id"]:
                    raise files.StorageUnavailable("Оригинал недоступен; текущая редакция не изменена")
                stage = owned_stage(db, user, session_hash, docid, item["stage_id"], valid_at=started_at)
                path = files.verified_stage(stage)
                db.commit()
                try:
                    provider.upload(path, item["storage_ref"], item["sha256"])
                except Exception:
                    raise files.StorageUnavailable(
                        "Хранилище не подтвердило сохранение оригинала; повторите запрос"
                    ) from None
                if not files.verify_remote(type("FileMetadata", (), item), provider):
                    raise files.StorageUnavailable("Хранилище не подтвердило SHA и размер оригинала")
            plan = {**plan, "uploaded": list(dict.fromkeys([*plan["uploaded"], item["id"]]))}
            attempt.plan = plan
            db.commit()  # Reconciliation knows which private originals were uploaded if SQL publication later fails.
        attempt.state = "uploaded"
        db.commit()
        # All rows, the current pointer, DOCUMENT version, and common HISTORY commit together.
        with db.begin_nested():
            reauthorize(db, user, session_hash, ptype, pid, docid)
            locked = ADAPTER.lock_record(db, docid)  # Existing lock order: kind -> document.
            if locked.version != expected_version:
                raise ObjectVersionConflictError("Документ изменён; черновик и прежняя редакция сохранены")
            reauthorize(db, user, session_hash, ptype, pid, docid)
            if any(db.get(DocumentFileCorrection, item["id"]) for item in plan["files"]):
                raise ValueError("Исправленный оригинал нельзя возвращать в рабочий комплект")
            if not all(files.verify_remote(type("FileMetadata", (), item)) for item in plan["files"]):
                raise files.StorageUnavailable(
                    "Перед публикацией доступность всего комплекта не подтверждена; текущая редакция не изменена"
                )
            if not plan["noop"]:
                revision = DocumentRevision(
                    id=plan["revision_id"],
                    document_id=docid,
                    number=plan["number"],
                    file_ids=[item["id"] for item in plan["files"]],
                    author_id=user.id,
                    source="arm2-api-correct" if correction else "arm2-api-manual",
                )
                db.add(revision)
                for item in plan["files"]:
                    if not item["existing"]:
                        db.add(
                            DocumentFile(
                                document_id=docid,
                                **{
                                    key: value
                                    for key, value in item.items()
                                    if key not in {"stage_id", "existing"}
                                },
                            )
                        )
                db.flush()
                db.add(DocumentRevisionContext(revision_id=revision.id, document_id=docid,
                                               parent_type=locked.parent_type, parent_id=locked.parent_id))
                for position, item in enumerate(plan["files"]):
                    db.add(
                        RevisionFile(
                            revision_id=revision.id, document_id=docid, file_id=item["id"], position=position
                        )
                    )
                db.flush()
            saved = update_object(
                db,
                object_type="DOCUMENT",
                object_id=docid,
                expected_version=expected_version,
                values={"current_revision_id": plan["revision_id"]},
                partial=True,
                principal=user,
                context=SaveContext("HUMAN", str(user.id), "arm2-api-correct" if correction else "arm2-api-manual", key),
            )
            if correction:
                if not saved.changed or saved.event is None:
                    raise ValueError("Исправление не создало согласованного события истории")
                db.add(DocumentFileCorrection(file_id=correction["file_id"], document_id=docid,
                     reason_code="incorrect_upload", operation="CORRECT", actor_id=user.id,
                     correlation_id=key, history_event_id=saved.event.event_id))
                db.flush()
            for item in plan["files"]:
                if item["stage_id"]:
                    db.get(FileStage, item["stage_id"]).state = "consumed"
            result = {
                "document_id": docid,
                "document_version": saved.record.version,
                "current_revision_id": saved.record.current_revision_id,
                "no_op": not saved.changed,
            }
            attempt.state = "accepted"
            attempt.result = result
        db.commit()
        return result


def cleanup_expired_staging(db):
    """Bounded, local-only cleanup; never remove a remote/accepted original or its ledger."""
    if not try_exclusive_file_access(db):
        return
    files.cleanup_reads()
    for stage in db.scalars(
        select(FileStage).where(FileStage.expires_at < instant()).limit(100).with_for_update(skip_locked=True)
    ):
        try:
            files.private_path(stage.id).unlink(missing_ok=True)
        except PermissionError:
            continue  # An active preview/download holds the Windows file handle.
    # Keep expired rows: safe reconciliation must retain ownership/provenance.
    from app.arm2.approval_models import ApprovalAttempt
    for attempt in db.scalars(select(ApprovalAttempt).where(
        ApprovalAttempt.action.in_(['letter', 'response-stage']),
        ApprovalAttempt.created_at < instant() - timedelta(seconds=files.STAGE_SECONDS))
        .limit(100).with_for_update(skip_locked=True)):
        plans = attempt.plan.get('files', []) if attempt.action == 'letter' else [attempt.plan['file']]
        for plan in plans:
            try:
                files.private_path(plan['token']).unlink(missing_ok=True)
            except PermissionError:
                continue


class StagingQuotaExceeded(ValueError):
    pass


def staging_quota(db, principal_id, extra_size, *, extra_count=1):
    """One private-runtime quota for manual B3 and process originals, never a business save."""
    from app.arm2.approval_models import ApprovalAttempt
    db.execute(text("SELECT pg_advisory_xact_lock(6365,:principal)"), {'principal': principal_id})
    cleanup_expired_staging(db)
    stages = db.scalars(select(FileStage).where(FileStage.principal_id == principal_id,
        FileStage.expires_at > instant(), FileStage.state != 'consumed')).all()
    plans = []
    for attempt in db.scalars(select(ApprovalAttempt).where(ApprovalAttempt.principal_id == principal_id,
        ApprovalAttempt.action.in_(['letter', 'response-stage']), ApprovalAttempt.result.is_(None),
        ApprovalAttempt.created_at > instant() - timedelta(seconds=files.STAGE_SECONDS))):
        plans.extend(attempt.plan.get('files', []) if attempt.action == 'letter' else [attempt.plan['file']])
    count = len(stages) + len(plans)
    size = sum(stage.size for stage in stages) + sum(plan['size'] for plan in plans)
    if count + extra_count > 64 or size + extra_size > files.settings().max_file_bytes * 8:
        raise StagingQuotaExceeded("Квота временных файлов исчерпана; дождитесь истечения срока кандидатов")


def publish_prepared_revision_in_transaction(db, prepared_plan, principal, context):
    """B5 internal publication boundary. Caller owns the ONE outer SQL commit.

    The prepared ledger contains private provider uploads with verified SHA/size, not
    arbitrary client FILE metadata. Nothing is downloaded/uploaded or committed here.
    Existing manual accept()/retry/correction behavior above remains unchanged.
    """
    from app.arm2.document_store import create_document

    p = prepared_plan
    if p.get('document_id'):
        document_access(db, principal, p['parent_type'], p['parent_id'], p['document_id'], edit=True)
        document = ADAPTER.lock_record(db, p['document_id'])
        if document.version != p['expected_version']:
            raise ObjectVersionConflictError('Редакция документа изменена другим оператором')
    else:
        document = create_document(db, p['parent_type'], p['parent_id'], p['kind_id'], p['document_name'], None,
                                   principal=principal, context=context)
        document_access(db, principal, p['parent_type'], p['parent_id'], document.id, edit=True)
    current = revision_files(db, document.id, document.current_revision_id)
    items = p['files']
    if len(items) > files.MAX_FILES or len({f['sha256'] for f in items}) != len(items):
        raise ValueError('Недопустимый состав комплекта')
    for item in items:
        if item['existing']:
            row = db.get(DocumentFile, item['id'])
            if row is None or row.document_id != document.id or row.sha256 != item['sha256']:
                raise ObjectAuthorizationError('FILE не принадлежит выбранному DOCUMENT')
        if db.get(DocumentFileCorrection, item['id']):
            raise ValueError('Исправленная ошибочная загрузка не может быть возвращена')
        if not item['storage_ref'].startswith(files.document_root() + '/') or not files.verify_remote(type('Metadata', (), item)):
            raise files.StorageUnavailable('Перед публикацией SHA/размер оригинала не подтверждены')
    if [r.id for r in current] == [r['id'] for r in items]:
        return {'document_id': document.id, 'version': document.version, 'no_op': True}
    previous = db.get(DocumentRevision, document.current_revision_id) if document.current_revision_id else None
    revision = DocumentRevision(id=next_id(db, 'DOCUMENT-REV'), document_id=document.id,
        number=previous.number + 1 if previous else 1, file_ids=[f['id'] for f in items], author_id=principal.id,
        source='arm2-api-manual')
    db.add(revision)
    for item in items:
        if not item['existing']:
            db.add(DocumentFile(document_id=document.id, **{k: item[k] for k in (
                'id', 'original_upload_name', 'stored_name', 'storage_ref', 'size', 'sha256', 'media_type', 'extension')}))
    db.flush()
    db.add(DocumentRevisionContext(revision_id=revision.id, document_id=document.id,
        parent_type=document.parent_type, parent_id=document.parent_id))
    for position, item in enumerate(items):
        db.add(RevisionFile(revision_id=revision.id, document_id=document.id, file_id=item['id'], position=position))
    db.flush()
    saved = update_object(db, object_type='DOCUMENT', object_id=document.id, expected_version=document.version,
        values={'current_revision_id': revision.id}, partial=True, principal=principal, context=context)
    if saved.event and set(saved.event.changes) - {'current_revision_id', 'version'}:
        raise ValueError('Невыбранные сведения документа требуют отдельного явного исправления')
    return {'document_id': document.id, 'version': saved.record.version, 'revision_id': revision.id, 'no_op': not saved.changed}
