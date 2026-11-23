from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_kind_store import create_document_kind as service_create_document_kind
from app.arm2.document_kind_store import update_document_kind as service_update_document_kind
from app.arm2.history_models import HistoryEvent
from app.arm2.history_store import HistoryValidationError, append_history_event
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectRegistryError, initialize_object_adapters
from app.arm2.object_save import (
    InvalidExpectedVersionError,
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.models import User
from app.permissions import (
    DEFAULT_OPERATOR_PERMISSIONS,
    Permission,
    effective_permissions,
    has_permission,
    permission_catalog,
    validate_permissions,
)

VIEW = Permission.ARM2_DOC_KIND_VIEW
EDIT = Permission.ARM2_DOC_KIND_EDIT
NEUTRAL = {"applicable_object_types": ["CP", "ORG", "VEH"], "max_current_documents": None, "file_naming_template": "DOCUMENT_NAME"}


def create_document_kind(db, name, *, principal, context):
    # Keep the #53 regression cases explicit about the newly mandatory settings.
    return service_create_document_kind(db, name, **NEUTRAL, principal=principal, context=context)


def update_document_kind(db, kind_id, version, name, *, principal, context):
    return service_update_document_kind(db, kind_id, version, name, **NEUTRAL, principal=principal, context=context)


@pytest.fixture
def session(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def user(db, login="doc-kind-editor", **overrides):
    principal = User(**({
        "login": login, "name": login, "password_hash": "unused", "role": "OPERATOR",
        "active": True, "permissions": [str(VIEW), str(EDIT)],
    } | overrides))
    db.add(principal)
    db.flush()
    return principal


def context(principal):
    return SaveContext("HUMAN", str(principal.id), "doc-kind-service-test", "kind-53")


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (DocumentKind, HistoryEvent))


def test_create_rename_noop_persistent_ids_and_real_author(session):
    db = session
    principal = user(db)
    first = create_document_kind(db, "  СТС\t", principal=principal, context=context(principal))
    second = create_document_kind(db, "Тарировка", principal=principal, context=context(principal))
    assert (first.id, second.id, first.name, first.version) == ("DOC-KIND-000001", "DOC-KIND-000002", "СТС", 1)
    created = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == first.id))
    assert (created.object_type, created.operation, created.actor_kind, created.actor_id, created.source,
            created.correlation_id, created.version_before, created.version_after) == (
        "DOC-KIND", "CREATED", "HUMAN", str(principal.id), "doc-kind-service-test", "kind-53", None, 1,
    )
    assert created.changes == {
        "name": {"before": None, "after": "СТС"}, "version": {"before": None, "after": 1},
        **{field: {"before": None, "after": value} for field, value in NEUTRAL.items()},
    }
    assert created.related_objects == []
    db.commit()
    result = update_document_kind(db, first.id, 1, "  Свидетельство  ", principal=principal, context=context(principal))
    assert (result.id, result.name, result.version) == (first.id, "Свидетельство", 2)
    changed = db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "UPDATED"))
    assert changed.changes == {
        "name": {"before": "СТС", "after": "Свидетельство"}, "version": {"before": 1, "after": 2},
    }
    assert (changed.version_before, changed.version_after) == (1, 2)
    no_op = update_object(db, object_type="DOC-KIND", object_id=first.id, expected_version=2,
                          values={"name": " Свидетельство ", **NEUTRAL}, principal=principal, context=context(principal))
    assert not no_op.changed and no_op.event is None and no_op.record.version == 2
    assert counts(db) == (2, 3)
    with pytest.raises(ObjectVersionConflictError):
        update_document_kind(db, first.id, 1, "Stale", principal=principal, context=context(principal))
    assert counts(db) == (2, 3)
    db.commit()
    db.expire_all()
    assert (db.get(DocumentKind, first.id).name, db.get(DocumentKind, first.id).version) == ("Свидетельство", 2)


def test_blank_duplicate_names_and_failed_update_are_atomic(session):
    db = session
    principal = user(db)
    for name in ["", " ", "\t\n\r", "\u00a0"]:
        with pytest.raises(ValueError):
            create_document_kind(db, name, principal=principal, context=context(principal))
    assert counts(db) == (0, 0)
    first = create_document_kind(db, "Первый", principal=principal, context=context(principal))
    second = create_document_kind(db, "Второй", principal=principal, context=context(principal))
    db.commit()
    with pytest.raises(IntegrityError):
        create_document_kind(db, " Первый ", principal=principal, context=context(principal))
    with pytest.raises(IntegrityError):
        update_document_kind(db, second.id, 1, " Первый ", principal=principal, context=context(principal))
    with pytest.raises(ValueError):
        update_document_kind(db, first.id, 1, "\t", principal=principal, context=context(principal))
    assert (db.get(DocumentKind, second.id).name, db.get(DocumentKind, second.id).version) == ("Второй", 1)
    assert counts(db) == (2, 2)


@pytest.mark.parametrize("overrides", [
    {"permissions": []}, {"permissions": [str(VIEW)]}, {"permissions": [str(EDIT)]},
    {"permissions": [str(Permission.ARM2_TYPE_VIEW), str(Permission.ARM2_TYPE_EDIT)]},
    {"active": False}, {"deleted_at": datetime(2026, 1, 1, tzinfo=UTC)},
])
def test_denied_create_and_update_do_not_save_or_append_events(session, overrides):
    db = session
    editor = user(db)
    existing = create_document_kind(db, "Сохранённый", principal=editor, context=context(editor))
    denied = user(db, "denied", **overrides)
    db.commit()
    for operation in [
        lambda: create_document_kind(db, "Denied", principal=denied, context=context(denied)),
        lambda: update_document_kind(db, existing.id, 1, "Denied", principal=denied, context=context(denied)),
    ]:
        with pytest.raises(ObjectAuthorizationError):
            operation()
    assert counts(db) == (1, 1)
    assert (existing.name, existing.version) == ("Сохранённый", 1)


def test_invalid_or_stale_principal_actor_and_expected_version_fail_closed(session):
    db = session
    principal = user(db)
    record = create_document_kind(db, "Сохранённый", principal=principal, context=context(principal))
    db.commit()
    for actor in [SaveContext("SYSTEM", str(principal.id), "test"), SaveContext("HUMAN", "999999", "test")]:
        with pytest.raises(ObjectAuthorizationError):
            create_document_kind(db, "Denied", principal=principal, context=actor)
        with pytest.raises(ObjectAuthorizationError):
            update_document_kind(db, record.id, 1, "Denied", principal=principal, context=actor)
    with pytest.raises(ObjectAuthorizationError):
        create_document_kind(db, "Denied", principal=None, context=context(principal))
    for version in [True, 0, -1, None, "1"]:
        with pytest.raises(InvalidExpectedVersionError):
            update_document_kind(db, record.id, version, "Denied", principal=principal, context=context(principal))
    # Shared SAVE reloads authority from the DB, ignoring a stale in-memory grant.
    db.execute(text("UPDATE users SET permissions='[]'::jsonb WHERE id=:id"), {"id": principal.id})
    with pytest.raises(ObjectAuthorizationError):
        update_document_kind(db, record.id, 1, "Denied", principal=principal, context=context(principal))
    assert counts(db) == (1, 1)


def test_registry_generic_save_id_immutability_and_history_validation(session):
    db = session
    principal = user(db)
    registry = initialize_object_adapters()
    adapter = registry.require("DOC-KIND").adapter
    assert initialize_object_adapters().require("DOC-KIND").adapter is adapter
    assert set(registry.require("DOC-KIND").command_fields) == {"name", *NEUTRAL}
    for values in [{"name": "Test", "id": "DOC-KIND-999999"}, {"name": "Test", "version": 5},
                   {"name": "Test", "revision_policy": "new"}, {"name": None}]:
        with pytest.raises(ObjectRegistryError):
            create_object(db, object_type="DOC-KIND", values={**NEUTRAL, **values}, principal=principal, context=context(principal))
    created = create_object(db, object_type="DOC-KIND", values={"name": "Generic", **NEUTRAL}, principal=principal, context=context(principal))
    assert created.event.object_type == "DOC-KIND" and created.record.version == 1
    with pytest.raises(ObjectRegistryError):
        update_object(db, object_type="DOC-KIND", object_id=created.record.id, expected_version=1,
                      values={"id": "DOC-KIND-999999"}, partial=True, principal=principal, context=context(principal))
    changes = {"name": {"before": None, "after": "Test"}, "version": {"before": None, "after": 1}}
    for invalid_id in ["TYPE-000001", "DOC-KIND-00001", "DOC-KIND-x00001"]:
        with pytest.raises(HistoryValidationError):
            append_history_event(db, object_type="DOC-KIND", object_id=invalid_id, operation="CREATED",
                                 actor_kind="HUMAN", actor_id=str(principal.id), source="test", version_after=1, changes=changes)
    with pytest.raises(HistoryValidationError):
        append_history_event(db, object_type="DOC-KIND", object_id=created.record.id, operation="CREATED",
                             actor_kind="HUMAN", actor_id=str(principal.id), source="test", version_after=1,
                             changes=changes | {"revision_policy": {"before": None, "after": "new"}})
    assert counts(db) == (1, 1)


def test_history_failure_rolls_back_document_kind_and_version(session):
    db = session
    principal = user(db)

    def fail_history(*_args, **_kwargs):
        raise RuntimeError("history unavailable")

    with pytest.raises(RuntimeError, match="history unavailable"):
        create_object(db, object_type="DOC-KIND", values={"name": "Failed", **NEUTRAL}, principal=principal,
                      context=context(principal), history_writer=fail_history)
    assert counts(db) == (0, 0)
    record = create_document_kind(db, "Before", principal=principal, context=context(principal))
    db.commit()
    with pytest.raises(RuntimeError, match="history unavailable"):
        update_object(db, object_type="DOC-KIND", object_id=record.id, expected_version=1,
                      values={"name": "Failed", **NEUTRAL}, principal=principal, context=context(principal), history_writer=fail_history)
    assert (record.name, record.version) == ("Before", 1)
    assert counts(db) == (1, 1)


def test_ids_are_non_reusable_concurrent_and_support_more_than_six_digits(session):
    db = session
    principal = user(db)
    first = create_document_kind(db, "First", principal=principal, context=context(principal))
    db.commit()
    user_id = principal.id
    rolled_back = create_document_kind(db, "Rolled back", principal=principal, context=context(principal)).id
    db.rollback()
    assert (first.id, rolled_back) == ("DOC-KIND-000001", "DOC-KIND-000002")
    engine = db.get_bind()

    def create_one(number):
        with Session(engine) as worker:
            editor = worker.get(User, user_id)
            record = create_document_kind(worker, f"Concurrent {number}", principal=editor, context=context(editor))
            result = record.id
            worker.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create_one, [1, 2]))
    assert set(ids) == {"DOC-KIND-000003", "DOC-KIND-000004"}
    db.execute(text("SELECT setval('arm2_document_kind_id_seq', 999999, true)"))
    record = create_document_kind(db, "Million", principal=principal, context=context(principal))
    assert record.id == "DOC-KIND-1000000"
    db.commit()
    assert counts(db) == (4, 4)


def test_concurrent_expected_version_has_one_winner(session):
    db = session
    principal = user(db)
    record = create_document_kind(db, "Before", principal=principal, context=context(principal))
    db.commit()
    user_id, kind_id, engine = principal.id, record.id, db.get_bind()

    def rename(number):
        with Session(engine) as worker:
            editor = worker.get(User, user_id)
            try:
                update_document_kind(worker, kind_id, 1, f"Winner {number}", principal=editor, context=context(editor))
                worker.commit()
                return "saved"
            except ObjectVersionConflictError:
                worker.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rename, [1, 2]))
    assert sorted(results) == ["conflict", "saved"]
    db.expire_all()
    assert db.get(DocumentKind, kind_id).version == 2
    assert counts(db) == (1, 2)


def test_db_enforces_five_fields_trim_uniqueness_id_and_positive_version(session):
    db = session
    assert [c["name"] for c in inspect(db.get_bind()).get_columns("arm2_document_kinds")] == ["id", "name", "version", *NEUTRAL]
    for row in [
        {"id": "TYPE-000001", "name": "Valid", "version": 1},
        {"id": "DOC-KIND-00001", "name": "Valid", "version": 1},
        {"id": "DOC-KIND-000001", "name": " ", "version": 1},
        {"id": "DOC-KIND-000001", "name": "\t", "version": 1},
        {"id": "DOC-KIND-000001", "name": "Trim ", "version": 1},
        {"id": "DOC-KIND-000001", "name": "Valid", "version": 0},
    ]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(DocumentKind(**row, **NEUTRAL))
            db.flush()
    assert counts(db) == (0, 0)


def test_permissions_have_prerequisite_and_are_not_default_grants():
    assert str(VIEW) not in DEFAULT_OPERATOR_PERMISSIONS and str(EDIT) not in DEFAULT_OPERATOR_PERMISSIONS
    normal = User(role="OPERATOR", permissions=[])
    assert not has_permission(normal, VIEW) and not has_permission(normal, EDIT)
    normal.permissions = [str(EDIT)]
    assert not has_permission(normal, EDIT)
    with pytest.raises(ValueError):
        validate_permissions([str(EDIT)])
    assert set(validate_permissions([str(VIEW), str(EDIT)])) == {VIEW, EDIT}
    normal.permissions = [str(VIEW), str(EDIT)]
    assert has_permission(normal, EDIT)
    assert Permission.ARM2_TYPE_EDIT not in effective_permissions(normal)
    entry = next(p for p in permission_catalog() if p["code"] == EDIT)
    assert entry["requires"] == [VIEW] and not entry["default_operator"]
    assert OBJECT_REGISTRY.require("DOC-KIND").edit_permission == EDIT
