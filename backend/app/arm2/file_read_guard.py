"""Serialize protected responses with retirement of their source records."""

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.arm2.object_save import ObjectVersionConflictError
from app.db import engine

# Database-local; held until ASGI completes the response (including streaming).
READ_KEY = 850910010


def protected_read():
    with Session(engine()) as db:
        if not db.scalar(text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": READ_KEY}):
            raise HTTPException(409, "Выполняется удаление объектов. Повторите операцию")
        yield


@contextmanager
def protected_background_work(database):
    # Separate lease transaction so stage/publication commits do not release it.
    with Session(database) as db:
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": READ_KEY})
        yield


def ensure_no_download(db):
    if not db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": READ_KEY}):
        raise ObjectVersionConflictError("Идёт выдача файлов или обработка связанных данных. Повторите после завершения операции")
