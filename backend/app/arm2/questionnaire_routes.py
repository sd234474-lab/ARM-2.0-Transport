"""Isolated B5 HTTP surface using existing authentication, CSRF and ARM2 permissions."""
import base64
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

from app.arm2 import document_file_storage as files
from app.arm2 import questionnaire_store as store
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError
from app.arm2.questionnaire_diff import authorized
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireReceipt, QuestionnaireUpload
from app.arm2.questionnaire_parse import MAX_COMPRESSED
from app.auth import CurrentUser, Db


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method not in {'GET', 'HEAD'}:
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_COMPRESSED:
                        raise HTTPException(413, 'Транспорт больше 32 МиБ')
                    chunks.append(chunk)
                request._body = b''.join(chunks)
            return await handler(request)
        return bounded


router = APIRouter(prefix='/questionnaires', route_class=BoundedRoute)


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RepresentativeChoice(Command):
    target: str | None = None
    fields: list[str] = Field(default_factory=list, max_length=6)
    signer: StrictBool = False
    sources: dict[str, Literal['passport', 'list']] = Field(default_factory=dict, max_length=3)


class VehicleChoice(Command):
    fields: list[str] = Field(default_factory=list, max_length=5)
    include: StrictBool = False
    type_id: str | None = None
    without_type: StrictBool = False
    confirm_owner: StrictBool = False


class DocumentChoice(Command):
    parent_key: str | None = None
    kind_id: str | None = None
    document_id: str | None = None
    replace_file_id: str | None = None
    append: StrictBool = False
    accept: StrictBool = False


class Choices(Command):
    cp_target: str | None = None
    cp_fields: list[str] = Field(default_factory=list, max_length=24)
    representatives: dict[str, RepresentativeChoice] = Field(default_factory=dict, max_length=1000)
    vehicles: dict[str, VehicleChoice] = Field(default_factory=dict, max_length=1000)
    documents: dict[str, DocumentChoice] = Field(default_factory=dict, max_length=512)


class Edit(Command):
    revision: Annotated[StrictInt, Field(gt=0)]
    choices: Choices


class Apply(Command):
    command_id: UUID
    revision: Annotated[StrictInt, Field(gt=0)]
    preview_id: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class Export(Command):
    format: Literal['xlsx', 'zip'] = 'xlsx'
    incomplete_reason: str | None = Field(default=None, min_length=1, max_length=500)


def translated(db, operation):
    try:
        return operation()
    except (ObjectAuthorizationError, ObjectNotFoundError) as error:
        db.rollback()
        raise HTTPException(403 if isinstance(error, ObjectAuthorizationError) else 404, str(error)) from None
    except ObjectVersionConflictError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Версии, права или уникальные связи изменены. Весь выбранный набор не сохранён; откройте сверку заново') from None
    except files.StorageUnavailable as error:
        db.rollback()
        raise HTTPException(503, str(error)) from None
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(409, 'SQL не подтвердил набор. Ни одно выбранное изменение не принято') from None


@router.post('/exports/{cp_id}')
def export(cp_id: str, body: Export, db: Db, user: CurrentUser):
    def operation():
        result = store.export(db, user, cp_id, with_files=body.format == 'zip', incomplete_reason=body.incomplete_reason)
        if 'data' in result:
            result['data'] = base64.b64encode(result['data']).decode('ascii')
        return result
    return translated(db, operation)


@router.get('')
def listing(db: Db, user: CurrentUser):
    def operation():
        authorized(db, user, 'arm2_cp_view')
        return [{'id': d.id, 'filename': d.filename, 'revision': d.revision, 'uploaded_at': d.uploaded_at.isoformat()}
                for d in db.scalars(select(QuestionnaireDraft).where(QuestionnaireDraft.author_id == user.id,
                    QuestionnaireDraft.status == 'OPEN').order_by(QuestionnaireDraft.uploaded_at.desc()))]
    return translated(db, operation)


@router.get('/receipts')
def receipts(db: Db, user: CurrentUser):
    def operation():
        authorized(db, user, 'arm2_cp_view')
        return [r.result for r in db.scalars(select(QuestionnaireReceipt).where(
            QuestionnaireReceipt.author_id == user.id).order_by(QuestionnaireReceipt.accepted_at.desc()).limit(100))]
    return translated(db, operation)


@router.get('/cleanup')
def cleanup_candidates(db: Db, user: CurrentUser):
    def operation():
        authorized(db, user, 'arm2_cp_view')
        return [{'id': d.id, 'filename': d.filename, 'status': d.status} for d in db.scalars(
            select(QuestionnaireDraft).join(QuestionnaireUpload, QuestionnaireUpload.draft_id == QuestionnaireDraft.id)
            .where(QuestionnaireDraft.author_id == user.id, QuestionnaireDraft.status != 'OPEN',
                   QuestionnaireUpload.state.not_in(['ACCEPTED', 'DISCARDED'])).distinct().order_by(QuestionnaireDraft.id))]
    return translated(db, operation)


@router.post('', status_code=201)
async def upload(request: Request, db: Db, user: CurrentUser,
                 filename: Annotated[str, Query(max_length=255)] = 'questionnaire.xlsx'):
    content = await request.body()
    return await run_in_threadpool(translated, db, lambda: store.upload(db, user, content, filename))


@router.get('/{identifier}')
def reopen(identifier: UUID, db: Db, user: CurrentUser):
    return translated(db, lambda: store.view(db, user, store.owned(db, user, str(identifier), open_only=False)))


@router.put('/{identifier}')
def edit(identifier: UUID, body: Edit, db: Db, user: CurrentUser):
    return translated(db, lambda: store.edit(db, user, str(identifier), body.revision, body.choices.model_dump()))


@router.post('/{identifier}/apply')
def apply(identifier: UUID, body: Apply, request: Request, db: Db, user: CurrentUser):
    session = getattr(request.state, 'login_session', None)
    return translated(db, lambda: store.apply(db, user, str(identifier), str(body.command_id), body.revision,
                                             body.preview_id, session_hash=session.token_hash if session else None))


@router.delete('/{identifier}')
def cancel(identifier: UUID, db: Db, user: CurrentUser):
    def operation():
        result = store.cancel(db, user, str(identifier))
        result['cleanup'] = store.cleanup(db, user, str(identifier))
        return result
    return translated(db, operation)


@router.post('/{identifier}/cleanup')
def cleanup(identifier: UUID, db: Db, user: CurrentUser):
    return translated(db, lambda: store.cleanup(db, user, str(identifier)))


@router.get('/{identifier}/files/{upload_id}/preview')
def preview(identifier: UUID, upload_id: str, db: Db, user: CurrentUser):
    def operation():
        store.owned(db, user, str(identifier))
        authorized(db, user, 'arm2_document_view')
        stage = db.get(QuestionnaireUpload, upload_id)
        if stage is None or stage.draft_id != str(identifier) or stage.error:
            raise ValueError('Проверенный файл не найден в вашем черновике')
        path = files.staged_preview(stage)
        return FileResponse(path, media_type=stage.media_type, headers={'Content-Security-Policy': 'sandbox',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
            background=BackgroundTask(path.unlink, missing_ok=True))
    return translated(db, operation)
