from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.counterparty_store import create_counterparty
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_kind_store import create_document_kind, update_document_kind
from app.arm2.document_models import Document
from app.arm2.document_store import create_document
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext, create_object, update_object
from app.models import User


@pytest.fixture
def data(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        user = User(
            login="document-concurrent",
            name="test",
            password_hash="unused",
            role="ADMIN_OPERATOR",
            active=True,
            permissions=[],
        )
        db.add(user)
        db.flush()
        ctx = SaveContext("HUMAN", str(user.id), "document-test")
        cp = create_counterparty(db, "CP", type='Юридическое лицо', principal=user, context=ctx)
        other = create_counterparty(db, "CP2", type='Юридическое лицо', principal=user, context=ctx)
        kind = create_document_kind(
            db,
            "Ограниченный",
            applicable_object_types=["CP"],
            max_current_documents=1,
            principal=user,
            context=ctx,
        )
        unlimited = create_document_kind(
            db,
            "Без лимита",
            applicable_object_types=["CP"],
            max_current_documents=None,
            principal=user,
            context=ctx,
        )
        result = (user.id, cp.id, other.id, kind.id, unlimited.id)
        db.commit()
    yield engine, result
    engine.dispose()


def test_two_concurrent_creates_limit_one_and_independent_parent(data):
    engine, (uid, cp, other, kind, unlimited) = data
    barrier = Barrier(2)

    def create(index):
        with Session(engine) as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            try:
                r = create_document(
                    db,
                    "CP",
                    cp,
                    kind,
                    f"Конкурент {index}",
                    None,
                    principal=user,
                    context=SaveContext("HUMAN", str(uid), "concurrent"),
                )
                result = r.id
                db.commit()
                return result
            except ValueError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [1, 2]))
    assert results.count("rejected") == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(HistoryEvent).where(HistoryEvent.object_type == "DOCUMENT")
            )
            == 1
        )
        user = db.get(User, uid)
        ctx = SaveContext("HUMAN", str(uid), "independent")
        create_document(db, "CP", other, kind, "Другой родитель", None, principal=user, context=ctx)
        for i in range(2):
            create_document(db, "CP", cp, unlimited, f"Без лимита {i}", None, principal=user, context=ctx)
        before = db.scalar(select(func.count()).select_from(HistoryEvent))
        with pytest.raises(ValueError, match="Правила не сохранены"):
            update_document_kind(
                db,
                unlimited,
                1,
                "Без лимита",
                applicable_object_types=["CP"],
                max_current_documents=1,
                principal=user,
                context=ctx,
            )
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before
        changed = update_document_kind(
            db,
            unlimited,
            1,
            "Предел 2",
            applicable_object_types=["CP"],
            max_current_documents=2,
            principal=user,
            context=ctx,
        )
        assert changed.version == 2
        same = update_document_kind(
            db,
            unlimited,
            2,
            "Предел 2",
            applicable_object_types=["CP"],
            max_current_documents=2,
            principal=user,
            context=ctx,
        )
        assert same.version == 2
        db.commit()


@pytest.mark.parametrize("change", ["parents", "maximum"])
def test_concurrent_kind_rules_and_create_are_serialized(data, change):
    engine, (uid, cp, _, _, kind) = data
    if change == "maximum":
        with Session(engine) as db:
            user = db.get(User, uid)
            create_document(
                db,
                "CP",
                cp,
                kind,
                "Первый",
                None,
                principal=user,
                context=SaveContext("HUMAN", str(uid), "seed"),
            )
            db.commit()
    barrier = Barrier(2)

    def run(operation):
        with Session(engine) as db:
            user = db.get(User, uid)
            # Keep a stale ORM copy deliberately: FOR UPDATE must refresh it.
            current = db.get(DocumentKind, kind)
            assert current.version == 1
            barrier.wait(timeout=10)
            ctx = SaveContext("HUMAN", str(uid), "race")
            try:
                if operation == "create":
                    create_document(db, "CP", cp, kind, "Новый", None, principal=user, context=ctx)
                else:
                    update_document_kind(
                        db,
                        kind,
                        1,
                        "Новые правила",
                        applicable_object_types=["ORG"] if change == "parents" else ["CP"],
                        max_current_documents=1 if change == "maximum" else None,
                        principal=user,
                        context=ctx,
                    )
                db.commit()
                return "ok"
            except ValueError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["create", "rules"]))
    assert sorted(results) == ["ok", "rejected"]
    with Session(engine) as db:
        current = db.get(DocumentKind, kind)
        count = db.scalar(select(func.count()).select_from(Document).where(Document.kind_ref == kind))
        if change == "parents":
            assert (count == 0 and current.applicable_object_types == ["ORG"]) or (
                count == 1 and current.applicable_object_types == ["CP"]
            )
        else:
            assert current.max_current_documents is None or count <= current.max_current_documents


def test_document_common_save_rolls_back_failed_history_and_does_not_reuse_ids(data):
    engine, (uid, cp, _, kind, _) = data

    def fail_history(*args, **kwargs):
        raise RuntimeError("history unavailable")

    with Session(engine) as db:
        user = db.get(User, uid)
        context = SaveContext("HUMAN", str(uid), "rollback-test")
        before = db.scalar(select(func.count()).select_from(HistoryEvent))
        with pytest.raises(RuntimeError, match="history unavailable"):
            create_object(
                db,
                object_type="DOCUMENT",
                values={
                    "parent_type": "CP",
                    "parent_id": cp,
                    "kind_ref": kind,
                    "name": "Не записывать",
                    "description": None,
                    "current_revision_id": None,
                    "archive_state": "Неархивный",
                },
                principal=user,
                context=context,
                history_writer=fail_history,
            )
        assert db.scalar(select(func.count()).select_from(Document)) == 0
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before
        record = create_document(db, "CP", cp, kind, "Первый", None, principal=user, context=context)
        assert record.id == "DOCUMENT-000002"
        with pytest.raises(RuntimeError, match="history unavailable"):
            update_object(
                db,
                object_type="DOCUMENT",
                object_id=record.id,
                expected_version=1,
                values={"name": "Не записывать", "description": "Нельзя"},
                partial=True,
                principal=user,
                context=context,
                history_writer=fail_history,
            )
        db.refresh(record)
        assert (record.name, record.description, record.version) == ("Первый", None, 1)
        assert db.scalar(select(func.count()).select_from(HistoryEvent)) == before + 1
        db.commit()
