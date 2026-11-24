"""The single protected API for marking and executing platform deletion."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.arm2 import deletion_service as service
from app.arm2.deletion_models import DeletionBatch
from app.arm2.http_errors import CONTROLLED_ERRORS, controlled_error, rollback
from app.arm2.http_limits import bounded_route
from app.arm2.object_save import ObjectVersionConflictError
from app.arm2.operation_models import BackgroundOperation
from app.auth import CurrentUser, Db

router = APIRouter(prefix="/deletion", route_class=bounded_route(128 * 1024, "Слишком большой набор удаления"))


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObjectKey(Command):
    object_type: str = Field(min_length=1, max_length=20)
    object_id: str = Field(min_length=1, max_length=80)


class MarkCommand(Command):
    expected_version: StrictInt = Field(gt=0)
    expected_mark: UUID | None
    marked: StrictBool


class Selection(Command):
    objects: list[ObjectKey] = Field(min_length=1, max_length=100)


class Confirmation(Command):
    confirmed: StrictBool


def transaction(db, action):
    try:
        result = action()
        db.commit()
        return result
    except CONTROLLED_ERRORS as error:
        rollback(db)
        raise controlled_error(error) from None
    except SQLAlchemyError as error:
        rollback(db)
        state = getattr(getattr(error, "orig", None), "sqlstate", None)
        if isinstance(error, IntegrityError) or state in {"55P03", "40P01", "40001"}:
            raise HTTPException(409, "Записи или связи изменились либо заняты. Проверьте набор заново") from None
        raise HTTPException(503, "База данных не подтвердила результат. Проверьте журнал удаления") from None


@router.get("/marks")
def marks(db: Db, user: CurrentUser, offset: Annotated[int, Query(ge=0)] = 0):
    return transaction(db, lambda: service.marked_objects(db, user, offset, 100))


@router.get("/marks/{kind}/{identifier}")
def get_mark(kind: str, identifier: str, db: Db, user: CurrentUser):
    return transaction(db, lambda: service.get_mark(db, user, (kind, identifier)))


@router.put("/marks/{kind}/{identifier}")
def put_mark(kind: str, identifier: str, body: MarkCommand, db: Db, user: CurrentUser):
    token = str(body.expected_mark) if body.expected_mark else None
    return transaction(db, lambda: service.write_mark(db, user, (kind, identifier), body.expected_version, token, body.marked))


@router.post("/preview")
def preview(body: Selection, db: Db, user: CurrentUser):
    return transaction(db, lambda: service.preview(db, user, [(key.object_type, key.object_id) for key in body.objects]))


@router.post("/batches/{batch_id}/execute")
def execute(batch_id: UUID, body: Confirmation, db: Db, user: CurrentUser):
    if not body.confirmed:
        raise HTTPException(422, "Требуется явное подтверждение удаления")
    return transaction(db, lambda: service.execute(db, user, str(batch_id)))


@router.get("/batches")
def journal(db: Db, user: CurrentUser, offset: Annotated[int, Query(ge=0)] = 0):
    def operation():
        service.authorize(db, user)
        rows = db.scalars(select(DeletionBatch).where(
            DeletionBatch.actor_id == user.id, DeletionBatch.executed_at.is_not(None)
        ).order_by(DeletionBatch.executed_at.desc(), DeletionBatch.id).offset(offset).limit(101)).all()
        return {"items": [service.receipt(db, row, user) for row in rows[:100]], "has_more": len(rows) > 100}

    return transaction(db, operation)


@router.post("/batches/{batch_id}/retry")
def retry_files(batch_id: UUID, db: Db, user: CurrentUser):
    def operation():
        service.authorize(db, user, execute=True, lock=True)
        batch = service.stored_batch(db, user, str(batch_id), lock=True)
        if not batch.executed_at:
            raise ObjectVersionConflictError("Удаление ещё не выполнено")
        db.execute(update(BackgroundOperation).where(
            BackgroundOperation.kind == service.CLEANUP,
            BackgroundOperation.payload["batch_id"].astext == batch.id,
            BackgroundOperation.state == "FAILED",
        ).values(state="QUEUED", failure=None, not_before=None))
        return service.receipt(db, batch, user)

    return transaction(db, operation)


@router.get("/batches/{batch_id}")
def batch(batch_id: UUID, db: Db, user: CurrentUser):
    def operation():
        service.authorize(db, user)
        return service.receipt(db, service.stored_batch(db, user, str(batch_id)), user)
    return transaction(db, operation)
