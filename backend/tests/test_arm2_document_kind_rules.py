import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_kind_store import create_document_kind, update_document_kind
from app.arm2.history_models import HistoryEvent
from app.arm2.object_registry import ObjectRegistryError
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.models import User


@pytest.fixture
def service(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine, expire_on_commit=False) as db:
        user = User(login="kind-rules", name="kind-rules", password_hash="unused", role="OPERATOR", active=True,
                    permissions=["arm2_doc_kind_view", "arm2_doc_kind_edit"])
        db.add(user)
        db.flush()
        yield db, user, SaveContext("HUMAN", str(user.id), "kind-rules-test")
    engine.dispose()


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (DocumentKind, HistoryEvent))


def test_settings_roundtrip_canonical_noop_diff_and_conflict(service):
    db, user, ctx = service
    vehicle = create_document_kind(db, "СТС", applicable_object_types=["VEH"], max_current_documents=1, principal=user, context=ctx)
    party = create_document_kind(db, "Доверенность", applicable_object_types=["ORG", "CP"], max_current_documents=None,
                                 principal=user, context=ctx)
    db.commit()
    db.expire_all()
    assert (vehicle.applicable_object_types, vehicle.max_current_documents) == (["VEH"], 1)
    assert (party.applicable_object_types, party.max_current_documents) == (["CP", "ORG"], None)
    assert (vehicle.id, party.id) == ("DOC-KIND-000001", "DOC-KIND-000002")
    event = db.scalar(select(HistoryEvent).where(HistoryEvent.object_id == vehicle.id))
    assert event.changes["applicable_object_types"] == {"before": None, "after": ["VEH"]}
    assert event.changes["max_current_documents"] == {"before": None, "after": 1}
    update_document_kind(db, vehicle.id, 1, "СТС", applicable_object_types=["ORG", "CP"], max_current_documents=None,
                         principal=user, context=ctx)
    assert vehicle.version == 2
    updated = db.scalar(select(HistoryEvent).where(HistoryEvent.operation == "UPDATED"))
    assert (updated.actor_id, updated.source) == (str(user.id), "kind-rules-test")
    assert updated.changes == {
        "applicable_object_types": {"before": ["VEH"], "after": ["CP", "ORG"]},
        "max_current_documents": {"before": 1, "after": None}, "version": {"before": 1, "after": 2},
    }
    update_document_kind(db, vehicle.id, 2, "СТС", applicable_object_types=["ORG", "CP"], max_current_documents=None,
                         principal=user, context=ctx)
    assert vehicle.version == 2 and counts(db) == (2, 3)
    with pytest.raises(ObjectVersionConflictError):
        update_document_kind(db, vehicle.id, 1, "СТС", applicable_object_types=["VEH"], max_current_documents=1,
                             principal=user, context=ctx)
    assert counts(db) == (2, 3)
    # Partial commands still go through the same normalizer and full shared SAVE.
    result = update_object(db, object_type="DOC-KIND", object_id=vehicle.id, expected_version=2,
                           values={"max_current_documents": 2}, partial=True, principal=user, context=ctx)
    assert result.changed and result.record.version == 3
    assert result.event.changes == {"max_current_documents": {"before": None, "after": 2}, "version": {"before": 2, "after": 3}}
    db.commit()
    db.expire_all()
    assert (vehicle.applicable_object_types, vehicle.max_current_documents) == (["CP", "ORG"], 2)
    update_document_kind(db, vehicle.id, 3, "СТС переименован", applicable_object_types=["VEH", "ORG", "CP"],
                         max_current_documents=2, principal=user, context=ctx)
    assert (vehicle.name, vehicle.applicable_object_types, vehicle.version) == ("СТС переименован", ["CP", "ORG", "VEH"], 4)
    assert counts(db) == (2, 5)
    latest = db.scalar(select(HistoryEvent).order_by(HistoryEvent.event_id.desc()))
    assert set(latest.changes) == {"name", "applicable_object_types", "version"}


@pytest.mark.parametrize("parents", [[], ["TYPE"], ["CP", "CP"], ["CP", "B5"], ["cp"], [""], "CP", None,
                                    ("CP",), {"CP"}, [True], [1], [{}], [["CP"]]])
def test_bad_parents_refused_on_create_and_update_without_side_effects(service, parents):
    db, user, ctx = service
    record = create_document_kind(db, "Before", applicable_object_types=["VEH"], max_current_documents=1, principal=user, context=ctx)
    for operation in [
        lambda: create_document_kind(db, "Denied", applicable_object_types=parents, max_current_documents=1, principal=user, context=ctx),
        lambda: update_document_kind(db, record.id, 1, "Denied", applicable_object_types=parents, max_current_documents=1, principal=user, context=ctx),
    ]:
        with pytest.raises(ValueError):
            operation()
    assert (record.name, record.applicable_object_types, record.max_current_documents, record.version) == ("Before", ["VEH"], 1, 1)
    assert counts(db) == (1, 1)


@pytest.mark.parametrize("maximum", [0, -1, True, False, "1", 1.5, [], {}])
def test_bad_maximum_refused_on_create_and_update_without_side_effects(service, maximum):
    db, user, ctx = service
    record = create_document_kind(db, "Before", applicable_object_types=["VEH"], max_current_documents=1, principal=user, context=ctx)
    for operation in [
        lambda: create_document_kind(db, "Denied", applicable_object_types=["CP"], max_current_documents=maximum, principal=user, context=ctx),
        lambda: update_document_kind(db, record.id, 1, "Denied", applicable_object_types=["CP"], max_current_documents=maximum, principal=user, context=ctx),
    ]:
        with pytest.raises(ValueError):
            operation()
    assert (record.name, record.applicable_object_types, record.max_current_documents, record.version) == ("Before", ["VEH"], 1, 1)
    assert counts(db) == (1, 1)


def test_create_requires_both_explicit_settings_and_rejects_future_fields(service):
    db, user, ctx = service
    for kwargs in [{}, {"applicable_object_types": ["CP"]}, {"max_current_documents": None}]:
        with pytest.raises(TypeError):
            create_document_kind(db, "Missing", principal=user, context=ctx, **kwargs)
    for values in [{"name": "Missing"}, {"name": "Missing", "applicable_object_types": ["CP"]},
                   {"name": "Future", "applicable_object_types": ["CP"], "max_current_documents": None, "revision_policy": "anything"}]:
        with pytest.raises(ObjectRegistryError):
            create_object(db, object_type="DOC-KIND", values=values, principal=user, context=ctx)
    assert counts(db) == (0, 0)


def test_failed_history_and_missing_edit_preserve_saved_rules(service):
    db, user, ctx = service
    record = create_document_kind(db, "Before", applicable_object_types=["VEH"], max_current_documents=1, principal=user, context=ctx)
    db.commit()

    def fail(*_args, **_kwargs):
        raise RuntimeError("history failure")

    with pytest.raises(RuntimeError, match="history failure"):
        update_object(db, object_type="DOC-KIND", object_id=record.id, expected_version=1,
                      values={"name": "After", "applicable_object_types": ["CP"], "max_current_documents": None},
                      principal=user, context=ctx, history_writer=fail)
    for permissions in [["arm2_doc_kind_view"], ["arm2_doc_kind_edit"]]:
        user.permissions = permissions
        db.commit()
        with pytest.raises(ObjectAuthorizationError):
            update_document_kind(db, record.id, 1, "Denied", applicable_object_types=["CP"], max_current_documents=None,
                                 principal=user, context=ctx)
    assert (record.name, record.applicable_object_types, record.max_current_documents, record.version) == ("Before", ["VEH"], 1, 1)
    assert counts(db) == (1, 1)


def test_database_accepts_exact_canonical_sets_and_blocks_saved_value_corruption(service):
    db, _, _ = service
    combinations = [["CP"], ["ORG"], ["VEH"], ["CP", "ORG"], ["CP", "VEH"], ["ORG", "VEH"], ["CP", "ORG", "VEH"]]
    for index, parents in enumerate(combinations, 1):
        db.add(DocumentKind(id=f"DOC-KIND-{index:06}", name=f"Direct {index}", version=1,
                            applicable_object_types=parents, max_current_documents=None))
    db.commit()
    for parents in [[], ["ORG", "CP"], ["CP", "CP"], ["TYPE"], [1], {}, "CP", None]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text("UPDATE arm2_document_kinds SET applicable_object_types=CAST(:value AS jsonb) WHERE id='DOC-KIND-000001'"),
                       {"value": json.dumps(parents)})
    for maximum in [0, -1]:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text("UPDATE arm2_document_kinds SET max_current_documents=:value WHERE id='DOC-KIND-000001'"), {"value": maximum})
    db.expire_all()
    assert db.get(DocumentKind, "DOC-KIND-000001").applicable_object_types == ["CP"]
    assert counts(db) == (7, 0)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text("INSERT INTO arm2_document_kinds (id,name) VALUES ('DOC-KIND-999999','Missing settings')"))
