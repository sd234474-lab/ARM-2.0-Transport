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
    DocumentRevision,
    FileAcceptance,
    FileStage,
    RevisionFile,
)
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.document_store import ADAPTER, PARENTS, parent_access
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


def owned_stage(db, user, session_hash, document_id, stage_id, *, ready=True):
    stage = db.get(FileStage, stage_id, populate_existing=True)
    if (
        stage is None
        or stage.document_id != document_id
        or stage.principal_id != user.id
        or stage.session_hash != session_hash
    ):
        raise ObjectAuthorizationError("Нет доступа к временному файлу")
    if stage.expires_at <= instant() or stage.state == "consumed":
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


def display_state(db, document):
    if document.archive_state == "Архивный":
        return "Архивный"
    current = revision_files(db, document.id, document.current_revision_id)
    if not current:
        return "Не загружен"
    # No stale positive cache: unknown, timeout and missing/corrupt bytes fail closed.
    deadline = db.info.setdefault("document_availability_deadline", time.monotonic() + 2)
    return (
        "Доступен"
        if all(time.monotonic() < deadline and files.verify_remote(row) for row in current)
        else "Файл недоступен"
    )


def public_file(row, position):
    return {
        key: getattr(row, key)
        for key in ["id", "original_upload_name", "stored_name", "size", "sha256", "media_type", "extension"]
    } | {"position": position}


def bundles(db, document):
    revisions = []
    for revision in db.scalars(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == document.id)
        .order_by(DocumentRevision.number.desc())
    ):
        revisions.append(
            {
                "id": revision.id,
                "number": revision.number,
                "document_id": document.id,
                "parent_type": document.parent_type,
                "parent_id": document.parent_id,
                "author_id": revision.author_id,
                "occurred_at": revision.occurred_at.isoformat(),
                "source": revision.source,
                "files": [
                    public_file(row, i) for i, row in enumerate(revision_files(db, document.id, revision.id))
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


def prepare(db, user, session_hash, document, expected_version, key, selection):
    fingerprint = hashlib.sha256(
        json.dumps(
            {"document": document.id, "expected_version": expected_version, "files": selection},
            sort_keys=True,
        ).encode()
    ).hexdigest()
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
                kind.file_naming_template, document, parent, kind, stage.file_id, stage.extension
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


def accept(db, user, session_hash, ptype, pid, docid, expected_version, key, selection):
    document = reauthorize(db, user, session_hash, ptype, pid, docid)
    with attempt_lease(db, key):
        attempt = prepare(db, user, session_hash, document, expected_version, key, selection)
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
                stage = owned_stage(db, user, session_hash, docid, item["stage_id"])
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
                    source="arm2-api-manual",
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
                context=SaveContext("HUMAN", str(user.id), "arm2-api-manual", key),
            )
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
    files.cleanup_reads()
    for stage in db.scalars(
        select(FileStage).where(FileStage.expires_at < instant()).limit(100).with_for_update(skip_locked=True)
    ):
        try:
            files.private_path(stage.id).unlink(missing_ok=True)
        except PermissionError:
            continue  # An active preview/download holds the Windows file handle.
    # Keep expired rows: safe reconciliation must retain ownership/provenance.
