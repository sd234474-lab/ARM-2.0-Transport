"""Shared controlled-error translation, deliberately independent of transactions.

Callers retain commit timing, process/storage responses and SQL outcome policy.
Never expose SQL messages or DB parameters through this mapper.
"""

from collections.abc import Mapping

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
)

CONTROLLED_ERRORS = (ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError, ValueError)
_STATUSES = (403, 404, 409, 422)


def controlled_error(error: Exception, messages: Mapping[type[Exception], str] | None = None) -> HTTPException:
    for kind, status in zip(CONTROLLED_ERRORS, _STATUSES, strict=True):
        if isinstance(error, kind):
            return HTTPException(status, (messages or {}).get(kind, str(error)))
    raise TypeError("Not an ARM2 controlled exception")


def rollback(db) -> None:
    """A broken connection must not replace the safe, original HTTP outcome."""
    try:
        db.rollback()
    except SQLAlchemyError:
        pass
