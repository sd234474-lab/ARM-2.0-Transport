"""All bytes are served by ARM, with current parent rights and scoped ownership."""

import hashlib
import os
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.arm2 import document_file_storage as files
from app.arm2.document_file_models import (
    DocumentFile,
    DocumentFileCorrection,
    DocumentRevision,
    FileStage,
    RevisionFile,
)
from app.arm2.document_file_store import (
    StagingQuotaExceeded,
    accept,
    bundles,
    document_access,
    export_name,
    instant,
    owned_stage,
    staging_quota,
)
from app.arm2.file_read_guard import attempt_lease, protected_read, stage_attempt_key
from app.arm2.http_errors import CONTROLLED_ERRORS, controlled_error, rollback
from app.arm2.ids import next_id
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError
from app.auth import CurrentUser, Db
from app.schemas import StrictModel

router = APIRouter()
Token = Literal["cp", "org", "veh"]
base = "/{parent_type}/{parent_id}/documents/{document_id}"


class StageInput(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    original_upload_name: str
    size: StrictInt = Field(gt=0)


class Candidate(StrictModel):
    stage_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    file_id: str | None = Field(default=None, pattern=r"^FILE-[0-9]{6,}$")


class AcceptInput(StrictModel):
    expected_version: StrictInt = Field(gt=0)
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")
    files: list[Candidate] = Field(max_length=files.MAX_FILES)


class CorrectInput(StrictModel):
    expected_version: StrictInt = Field(gt=0)
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")
    file_id: str = Field(pattern=r"^FILE-[0-9]{6,}$")
    stage_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def translated(db, operation):
    try:
        return operation()
    except HTTPException:
        rollback(db)
        raise
    except StagingQuotaExceeded as error:
        rollback(db)
        raise HTTPException(413, str(error)) from None
    except CONTROLLED_ERRORS as error:
        rollback(db)
        raise controlled_error(error) from None
    except files.StorageUnavailable as error:
        rollback(db)
        raise HTTPException(503, str(error)) from None
    except SQLAlchemyError:
        # SQL errors may carry private storage parameters. An uncertain commit is
        # resolved by the same idempotency key, never by blind publication/reupload.
        rollback(db)
        raise HTTPException(503, "Сохранение не подтверждено; повторите запрос с тем же ключом") from None


def stage_data(stage, db=None, document=None):
    data = {
        key: getattr(stage, key)
        for key in ["id", "original_upload_name", "size", "sha256", "media_type", "extension", "state"]
    }

    if stage.extension and document is not None:
        from types import SimpleNamespace
        data['download_name'] = export_name(db, document, SimpleNamespace(id=stage.file_id, extension=stage.extension), revision_id=None)
    return data


@router.get(base + "/files")
def get_files(parent_type: Token, parent_id: str, document_id: str, db: Db, user: CurrentUser, history: bool = True):
    return translated(
        db, lambda: bundles(db, document_access(db, user, parent_type.upper(), parent_id, document_id), history, user=user)
    )


@router.post(base + "/stages", status_code=201)
def start_stage(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    body: StageInput,
    request: Request,
    db: Db,
    user: CurrentUser,
):
    def operation():
        document_access(db, user, parent_type.upper(), parent_id, document_id, edit=True)
        name = files.original_name(body.original_upload_name)
        if body.size > files.settings().max_file_bytes:
            raise HTTPException(413, "Файл превышает установленный предел")
        staging_quota(db, user.id, body.size)
        stage = FileStage(
            id=secrets.token_hex(32),
            document_id=document_id,
            principal_id=user.id,
            session_hash=request.state.login_session.token_hash,
            file_id=next_id(db, "FILE"),
            original_upload_name=name,
            size=body.size,
            state="empty",
            expires_at=instant() + timedelta(seconds=files.STAGE_SECONDS),
        )
        db.add(stage)
        db.commit()
        return stage_data(stage)

    return translated(db, operation)


@router.get(base + "/stages/{stage_id}")
def get_stage(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    stage_id: str,
    request: Request,
    db: Db,
    user: CurrentUser,
):
    def operation():
        document = document_access(db, user, parent_type.upper(), parent_id, document_id)
        return stage_data(
            owned_stage(db, user, request.state.login_session.token_hash, document_id, stage_id, ready=False), db, document
        )

    return translated(db, operation)


@router.put(base + "/stages/{stage_id}/content")
async def upload_stage(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    stage_id: str,
    request: Request,
    db: Db,
    user: CurrentUser,
):
    translated(db, lambda: document_access(db, user, parent_type.upper(), parent_id, document_id, edit=True))
    stage = translated(
        db,
        lambda: owned_stage(
            db, user, request.state.login_session.token_hash, document_id, stage_id, ready=False
        ),
    )
    if stage.state != "empty":
        raise HTTPException(409, "Временный файл уже зафиксирован; выберите новый кандидат")
    path = files.private_path(stage.id)
    owns_file = False
    try:
        with attempt_lease(db, stage_attempt_key(stage.id)):
            document_access(db, user, parent_type.upper(), parent_id, document_id, edit=True)
            stage = owned_stage(
                db, user, request.state.login_session.token_hash, document_id, stage_id, ready=False
            )
            if stage.state != "empty":
                raise HTTPException(409, "Временный файл уже зафиксирован")
            owns_file = True
            # A crashed upload may leave only this private, owned temporary object.
            path.unlink(missing_ok=True)
            received = 0
            result = hashlib.sha256()
            with path.open("xb") as target:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > stage.size or received > files.settings().max_file_bytes:
                        raise HTTPException(413, "Файл превышает заявленный размер")
                    target.write(chunk)
                    result.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if received != stage.size:
                raise ValueError("Файл передан не полностью")
            media, extension = files.content_type(path)
            # Check live grants again after the bounded body has arrived.
            document_access(db, user, parent_type.upper(), parent_id, document_id, edit=True)
            stage.sha256 = result.hexdigest()
            stage.media_type, stage.extension, stage.state = media, extension, "ready"
            db.commit()
            return stage_data(stage, db, document_access(db, user, parent_type.upper(), parent_id, document_id))
    except (
        HTTPException,
        ValueError,
        ObjectAuthorizationError,
        ObjectVersionConflictError,
        files.StorageUnavailable,
    ) as error:
        if owns_file:
            path.unlink(missing_ok=True)
        return translated(db, lambda error=error: raise_error(error))
    except SQLAlchemyError as error:
        # Keep complete private bytes if the commit acknowledgement was lost.
        return translated(db, lambda error=error: raise_error(error))
    except BaseException:
        if owns_file:
            path.unlink(missing_ok=True)
        db.rollback()
        raise


def raise_error(error):
    raise error


def protected_response(path, name, media, preview, *, temporary=False):
    supported = media == "application/pdf" or media.startswith("image/")
    return FileResponse(
        path,
        media_type=media,
        filename=name,
        content_disposition_type="inline" if preview and supported else "attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
        background=BackgroundTask(path.unlink, missing_ok=True) if temporary else None,
    )


@router.get(base + "/stages/{stage_id}/{action}", dependencies=[Depends(protected_read, scope="request")])
def preview_stage(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    stage_id: str,
    action: Literal["preview", "download"],
    request: Request,
    db: Db,
    user: CurrentUser,
):
    def operation():
        document = document_access(db, user, parent_type.upper(), parent_id, document_id)
        stage = owned_stage(db, user, request.state.login_session.token_hash, document_id, stage_id)
        path = files.staged_preview(stage)
        # The original client filename is never used as a path or an executable extension.
        name = stage_data(stage, db, document)["download_name"]
        return protected_response(path, name, stage.media_type, action == "preview", temporary=True)

    return translated(db, operation)


@router.post(base + "/accept", dependencies=[Depends(protected_read, scope="request")])
def accept_files(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    body: AcceptInput,
    request: Request,
    db: Db,
    user: CurrentUser,
):
    selection = [item.model_dump(exclude_none=True) for item in body.files]
    if any(len(item) != 1 for item in selection):
        raise HTTPException(422, "Выберите ровно один ID исходного или временного файла для каждой строки")
    return translated(
        db,
        lambda: accept(
            db,
            user,
            request.state.login_session.token_hash,
            parent_type.upper(),
            parent_id,
            document_id,
            body.expected_version,
            body.idempotency_key,
            selection,
        ),
    )


@router.get(base + "/revisions/{revision_id}/files/{file_id}/{action}",
            dependencies=[Depends(protected_read, scope="request")])
def read_file(
    parent_type: Token,
    parent_id: str,
    document_id: str,
    revision_id: str,
    file_id: str,
    action: Literal["preview", "download"],
    db: Db,
    user: CurrentUser,
):
    def operation():
        document = document_access(db, user, parent_type.upper(), parent_id, document_id)
        revision = db.get(DocumentRevision, revision_id)
        row = db.scalar(
            select(DocumentFile)
            .join(RevisionFile, RevisionFile.file_id == DocumentFile.id)
            .where(
                RevisionFile.revision_id == revision_id,
                RevisionFile.file_id == file_id,
                RevisionFile.document_id == document_id,
            )
        )
        if revision is None or revision.document_id != document_id or row is None or db.get(DocumentFileCorrection, file_id):
            raise ObjectNotFoundError("Файл не входит в указанную редакцию этого документа")
        name = export_name(db, document, row, revision_id=revision_id)
        path = files.download(row)
        return protected_response(path, files.verified_download_name(name, path), row.media_type,
                                  action == "preview", temporary=True)

    return translated(db, operation)


@router.post(base + "/corrections", dependencies=[Depends(protected_read, scope="request")])
def correct_file(parent_type: Token, parent_id: str, document_id: str, body: CorrectInput,
                 request: Request, db: Db, user: CurrentUser):
    return translated(db, lambda: accept(db, user, request.state.login_session.token_hash,
        parent_type.upper(), parent_id, document_id, body.expected_version, body.idempotency_key,
        None, {"file_id": body.file_id, "stage_id": body.stage_id}))
