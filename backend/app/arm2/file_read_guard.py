"""Serialize protected responses with retirement of their source records."""

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.arm2.object_save import ObjectVersionConflictError
from app.db import engine

# Database-local; held until ASGI completes the response (including streaming).
READ_KEY = 850910010
ATTEMPT_LOCK = "hashtextextended(:key,6365)"


def stage_attempt_key(identifier):
    return "stage-" + identifier


def approval_attempt_key(identifier):
    return "approval-" + identifier


@contextmanager
def attempt_lease(db, key):
    # Session lock spans provider I/O and survives commits by the operation.
    with db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        if not lock.scalar(text(f"SELECT pg_try_advisory_lock({ATTEMPT_LOCK})"), {"key": key}):
            raise ObjectVersionConflictError("Запрос уже выполняется; повторите с тем же ключом")
        try:
            yield
        finally:
            lock.execute(text(f"SELECT pg_advisory_unlock({ATTEMPT_LOCK})"), {"key": key})


def try_idle_attempt(db, key):
    return db.scalar(text(f"SELECT pg_try_advisory_xact_lock({ATTEMPT_LOCK})"), {"key": key})


@contextmanager
def protected_file_read(database):
    with Session(database) as db:
        if not db.scalar(text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": READ_KEY}):
            raise HTTPException(409, "Выполняется удаление объектов. Повторите операцию")
        yield


def protected_read():
    with protected_file_read(engine()):
        yield


@contextmanager
def protected_background_work(database):
    # Separate lease transaction so stage/publication commits do not release it.
    with Session(database) as db:
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": READ_KEY})
        yield


def try_exclusive_file_access(db):
    return db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": READ_KEY})


def ensure_no_download(db):
    if not try_exclusive_file_access(db):
        raise ObjectVersionConflictError("Идёт выдача файлов или обработка связанных данных. Повторите после завершения операции")


@contextmanager
def protected_cleanup(database):
    """Exclude publication and protected file reads during a cleanup attempt."""
    with database.connect().execution_options(isolation_level="AUTOCOMMIT") as lease:
        if not lease.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": READ_KEY}):
            raise ObjectVersionConflictError("Файлы используются другой операцией")
        try:
            yield
        finally:
            lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": READ_KEY})
