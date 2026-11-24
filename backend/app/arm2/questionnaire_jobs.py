"""Background adapter for the existing atomic questionnaire acceptance."""
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.arm2 import document_file_storage as files
from app.arm2 import questionnaire_store as store
from app.arm2.document_file_store import attempt_lease
from app.arm2.file_read_guard import protected_background_work
from app.arm2.http_errors import CONTROLLED_ERRORS, controlled_error, rollback
from app.arm2.object_save import ObjectAuthorizationError, ObjectVersionConflictError
from app.arm2.operation_models import BackgroundOperation
from app.arm2.questionnaire_diff import authorized
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireReceipt
from app.models import LoginSession, User

log = logging.getLogger(__name__)


def receipt_for(db, job):
    receipt = db.get(QuestionnaireReceipt, job.payload['command_id'])
    return receipt.result if receipt and receipt.author_id == job.author_id and receipt.draft_id == job.resource_id else None


def status(db, job, draft, *, detail=False):
    receipt = receipt_for(db, job) if detail else None
    return {
        'id': job.id, 'draft_id': job.resource_id, 'filename': draft.filename, 'revision': job.payload['revision'],
        'state': 'SUCCEEDED' if receipt else job.state, 'progress': job.progress,
        'failure': None if receipt else job.failure, 'updated_at': job.updated_at.isoformat(),
        'without_documents': job.payload['without_documents'],
        **({'receipt': receipt} if detail else {}),
    }


def latest(db, identifier):
    return db.scalar(select(BackgroundOperation).where(
        BackgroundOperation.kind == store.APPLY_OPERATION, BackgroundOperation.resource_id == identifier,
    ).order_by(BackgroundOperation.created_at.desc(), BackgroundOperation.id.desc()).limit(1))


def state_for(db, user, identifier):
    draft = store.owned(db, user, identifier, open_only=False, lock=False)
    job = latest(db, identifier)
    return status(db, job, draft, detail=True) if job else None


def journal(db, user):
    authorized(db, user, 'arm2_cp_view')
    latest_ids = select(BackgroundOperation.id).where(
        BackgroundOperation.kind == store.APPLY_OPERATION, BackgroundOperation.author_id == user.id,
    ).distinct(BackgroundOperation.resource_id).order_by(
        BackgroundOperation.resource_id, BackgroundOperation.created_at.desc(), BackgroundOperation.id.desc(),
    )
    rows = db.execute(select(BackgroundOperation, QuestionnaireDraft).join(
        QuestionnaireDraft, QuestionnaireDraft.id == BackgroundOperation.resource_id,
    ).where(BackgroundOperation.id.in_(latest_ids), QuestionnaireDraft.author_id == user.id)
        .order_by(BackgroundOperation.updated_at.desc()).limit(100))
    return [status(db, job, draft) for job, draft in rows]


def enqueue(db, user, identifier, operation_id, revision, preview_id, session_hash, without_documents):
    draft = store.owned(db, user, identifier, open_only=False, lock=False)
    selection = {'revision': revision, 'preview_id': preview_id, 'without_documents': without_documents}

    def existing():
        prior = db.get(BackgroundOperation, operation_id)
        if prior:
            if (prior.kind != store.APPLY_OPERATION or prior.author_id != user.id or prior.resource_id != identifier
                    or any(prior.payload[key] != value for key, value in selection.items())):
                raise ObjectVersionConflictError('Команда уже связана с другой сверкой')
            return prior
        return store.active_apply(db, identifier)

    if job := existing():
        return status(db, job, draft, detail=True)
    try:
        with attempt_lease(db, 'B5:' + identifier):
            draft = store.owned(db, user, identifier, open_only=False)
            if job := existing():
                return status(db, job, draft, detail=True)
            previous = latest(db, identifier)
            if draft.status == 'ACCEPTED' and previous and receipt_for(db, previous):
                return status(db, previous, draft, detail=True)
            if (draft.status != 'OPEN' or draft.revision != revision or not draft.preview
                    or store.review_hash(draft.preview, draft.choices) != preview_id):
                raise ObjectVersionConflictError('Сверка изменилась. Откройте её и проверьте решения заново')
            # Retry a failed set with the existing upload ledger and permanent destinations.
            reuse = previous and previous.state == 'FAILED' and all(
                previous.payload[key] == value for key, value in selection.items())
            command_id = previous.payload['command_id'] if reuse else operation_id
            job = BackgroundOperation(id=operation_id, kind=store.APPLY_OPERATION, resource_id=identifier,
                author_id=user.id, state='QUEUED', payload={**selection, 'command_id': command_id, 'session_hash': session_hash},
                progress={'phase': 'QUEUED'})
            db.add(job)
            db.commit()
            return status(db, job, draft, detail=True)
    except ObjectVersionConflictError:
        # The worker or a second tab may have started after the initial lookup.
        db.rollback()
        if job := existing():
            return status(db, job, draft, detail=True)
        raise


def apply_job(database, job, report):
    with protected_background_work(database), Session(database, expire_on_commit=False) as db:
        if receipt_for(db, job):
            return None
        payload = job.payload
        try:
            user = db.get(User, job.author_id)
            if not user:
                raise ObjectAuthorizationError('Доступ к приёмке отозван')
            if payload['session_hash']:
                session = db.get(LoginSession, payload['session_hash'])
                if not session or session.user_id != user.id or session.expires_at <= datetime.now(UTC):
                    raise ObjectAuthorizationError('Сессия завершилась. Войдите и повторите приёмку')
            store.apply(db, user, job.resource_id, payload['command_id'], payload['revision'], payload['preview_id'],
                session_hash=payload['session_hash'], without_documents=payload['without_documents'],
                operation_id=job.id, progress=report)
            return None
        except Exception as error:
            rollback(db)
            # Receipt is committed together with all domain changes. If the database
            # is unreachable here, leave RUNNING for recovery instead of guessing.
            with Session(database) as check:
                if receipt_for(check, job):
                    return None
            if isinstance(error, store.QuestionnaireDiskUnavailable):
                return {'code': 'STORAGE', 'message': str(error), 'retryable': True, 'can_save_without_documents': True}
            if isinstance(error, files.StorageUnavailable):
                return {'code': 'ATTACHMENT', 'message': str(error), 'retryable': False}
            if isinstance(error, CONTROLLED_ERRORS):
                mapped = controlled_error(error)
                return {'code': 'REVIEW', 'message': mapped.detail, 'retryable': mapped.status_code == 403}
            if isinstance(error, IntegrityError):
                return {'code': 'REVIEW', 'message': 'Версии или связи изменились. Откройте сверку заново', 'retryable': False}
            log.warning('arm2_questionnaire_apply_error=%s', type(error).__name__)
            message = ('База данных не подтвердила сохранение' if isinstance(error, SQLAlchemyError)
                       else 'Внутренний сбой при приёмке анкеты')
            return {'code': 'SERVER', 'message': message, 'retryable': True}
