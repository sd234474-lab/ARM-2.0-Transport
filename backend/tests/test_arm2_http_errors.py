"""Common HTTP mapping with explicit, unchanged per-process transaction policies."""

from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.arm2 import document_file_routes as b3
from app.arm2 import questionnaire_routes as b5
from app.arm2 import routes
from app.arm2.document_file_storage import StorageUnavailable
from app.arm2.http_errors import controlled_error
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError, ObjectVersionConflictError
from app.arm2.questionnaire_store import QuestionnaireDiskUnavailable


def fail(error):
    def operation():
        raise error
    return operation


@pytest.mark.parametrize("wrapper", [routes._save, b3.translated, b5.translated])
@pytest.mark.parametrize("kind,status", [
    (ObjectAuthorizationError, 403), (ObjectNotFoundError, 404),
    (ObjectVersionConflictError, 409), (ValueError, 422),
])
def test_common_mapping_with_consumer_messages_and_one_rollback(wrapper, kind, status):
    db = Mock()
    with pytest.raises(HTTPException) as caught:
        wrapper(db, fail(kind("controlled detail")))
    assert caught.value.status_code == status
    assert caught.value.detail == (
        {403: "ARM2 write permission denied", 404: "ARM2 object not found", 409: "ARM2 object version conflict"}.get(status, "controlled detail")
        if wrapper is routes._save else "controlled detail"
    )
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


@pytest.mark.parametrize("wrapper,status", [(routes._save, 500), (b3.translated, 503), (b5.translated, 409)])
def test_sql_errors_and_failed_rollback_never_leak_parameters(wrapper, status):
    db = Mock()
    db.rollback.side_effect = SQLAlchemyError("secret rollback parameters")
    with pytest.raises(HTTPException) as caught:
        wrapper(db, fail(IntegrityError("secret SQL", {"token": "secret token"}, Exception("secret driver"))))
    assert caught.value.status_code == status
    assert "secret" not in str(caught.value.detail)
    assert caught.value.__suppress_context__
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_commit_ownership_and_uncertain_commit_are_not_unified_or_retried():
    for wrapper in [routes._save, b3.translated, b5.translated]:
        db = Mock()
        assert wrapper(db, lambda: "result") == "result"
        assert db.commit.call_count == (1 if wrapper is routes._save else 0)
        db.rollback.assert_not_called()
    db = Mock()
    db.commit.side_effect = SQLAlchemyError("secret commit outcome")
    operation = Mock(return_value="candidate")
    with pytest.raises(HTTPException) as caught:
        routes._save(db, operation)
    assert caught.value.status_code == 500
    operation.assert_called_once()
    db.commit.assert_called_once()
    db.rollback.assert_called_once()


def test_file_uncertainty_and_questionnaire_storage_fallback_keep_distinct_contracts():
    with pytest.raises(HTTPException) as caught:
        b3.translated(Mock(), fail(SQLAlchemyError("secret")))
    assert caught.value.status_code == 503
    assert "тем же ключом" in caught.value.detail
    with pytest.raises(HTTPException) as caught:
        b5.translated(Mock(), fail(QuestionnaireDiskUnavailable("Хранилище недоступно")), questionnaire_apply=True)
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "QUESTIONNAIRE_STORAGE_UNAVAILABLE", "message": "Хранилище недоступно", "can_save_without_documents": True}
    for wrapper in [b3.translated, b5.translated]:
        with pytest.raises(HTTPException) as caught:
            wrapper(Mock(), fail(StorageUnavailable("Недоступно")))
        assert (caught.value.status_code, caught.value.detail) == (503, "Недоступно")


def test_mapper_rejects_database_errors_instead_of_serializing_them():
    with pytest.raises(TypeError):
        controlled_error(SQLAlchemyError("secret parameters"))
