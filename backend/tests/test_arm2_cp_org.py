import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
)
from app.arm2.organization_store import create_organization, update_organization
from app.arm2.representative_store import create_representative
from app.models import User
from app.permissions import Permission


def user(db, login="cp-org-admin", role="ADMIN_OPERATOR", permissions=None):
    record = User(login=login, name=login, password_hash="unused", role=role, active=True, permissions=permissions or [])
    db.add(record)
    db.flush()
    return record


def context(record): return SaveContext("HUMAN", str(record.id), "cp-org-test")


def test_cp_org_status_validation_ids_permissions_and_inn(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        admin = user(db)
        viewer = user(db, "cp-viewer", "OPERATOR", [str(Permission.ARM2_CP_VIEW)])
        with pytest.raises(ObjectAuthorizationError):
            create_counterparty(db, "Denied", principal=viewer, context=context(viewer))
        cp = create_counterparty(db, "  Same name  ", principal=admin, context=context(admin))
        org = create_organization(db, "Same name", principal=admin, context=context(admin))
        assert (cp.id, cp.name, cp.status) == ("CP-000001", "Same name", "REVIEW")
        assert (org.id, org.status) == ("ORG-000001", "REVIEW")
        cp = update_counterparty(db, cp.id, 1, cp.name, full_name="Full CP", legal_address="Address",
                                 inn="001234567890", principal=admin, context=context(admin))
        org = update_organization(db, org.id, 1, org.name, full_name="Full ORG", legal_address="Address",
                                  inn="001234567890", principal=admin, context=context(admin))
        assert (cp.status, org.status) == ("CONFIRMED", "CONFIRMED")
        cp = update_counterparty(db, cp.id, 2, cp.name, full_name=None, legal_address="Address",
                                 inn="001234567890", principal=admin, context=context(admin))
        assert cp.status == "REVIEW"
        db.commit()
        with pytest.raises(ValueError, match="inn"):
            update_organization(db, org.id, 2, org.name, full_name="Full", legal_address="Address",
                                inn="12A", principal=admin, context=context(admin))
        db.rollback()
    with Session(engine) as db:
        admin = db.scalar(select(User).where(User.login == "cp-org-admin"))
        create_counterparty(db, "One", inn="1234567890", principal=admin, context=context(admin))
        with pytest.raises(IntegrityError):
            create_counterparty(db, "Two", inn="1234567890", principal=admin, context=context(admin))
        db.rollback()
    engine.dispose()


def test_cp_org_partial_update_preserves_data_and_history(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        admin = user(db, "partial-admin")
        viewer = user(db, "partial-viewer", "OPERATOR", [str(Permission.ARM2_CP_VIEW)])
        details = {
            "full_name": "Full CP",
            "legal_address": "Legal address",
            "inn": "1234567890",
            "email": "cp@example.ru",
            "bank_name": "Bank",
            "bik": "123456789",
            "settlement_account": "1" * 20,
            "correspondent_account": "2" * 20,
        }
        cp = create_counterparty(
            db, "CP", principal=admin, context=context(admin), **details
        )
        org = create_organization(
            db,
            "ORG",
            full_name="Full ORG",
            legal_address="ORG address",
            inn="123456789012",
            email="org@example.ru",
            bank_name="ORG bank",
            principal=admin,
            context=context(admin),
        )
        signer = create_representative(
            db, "CP", cp.id, "Signer", principal=admin, context=context(admin)
        )

        cp = update_counterparty(
            db,
            cp.id,
            1,
            cp.name,
            phone="+7 000 000-00-00",
            principal=admin,
            context=context(admin),
        )
        assert cp.version == 2 and cp.status == "CONFIRMED"
        for field, value in details.items():
            assert getattr(cp, field) == value
        cp = update_counterparty(
            db,
            cp.id,
            2,
            cp.name,
            primary_signer_id=signer.id,
            principal=admin,
            context=context(admin),
        )
        assert cp.primary_signer_id == signer.id and cp.status == "CONFIRMED"
        assert cp.full_name == details["full_name"] and cp.inn == details["inn"]
        cp = update_counterparty(
            db,
            cp.id,
            3,
            cp.name,
            primary_signer_id=None,
            principal=admin,
            context=context(admin),
        )
        assert cp.primary_signer_id is None and cp.full_name == details["full_name"]

        org = update_organization(
            db,
            org.id,
            1,
            org.name,
            phone="+7 111 111-11-11",
            principal=admin,
            context=context(admin),
        )
        assert (org.version, org.status, org.inn, org.bank_name) == (
            2,
            "CONFIRMED",
            "123456789012",
            "ORG bank",
        )

        unchanged = update_counterparty(
            db,
            cp.id,
            4,
            cp.name,
            principal=admin,
            context=context(admin),
        )
        assert unchanged.version == 4
        with pytest.raises(ObjectVersionConflictError):
            update_counterparty(
                db,
                cp.id,
                3,
                cp.name,
                principal=admin,
                context=context(admin),
            )
        with pytest.raises(ObjectAuthorizationError):
            update_counterparty(
                db,
                "CP-999999",
                1,
                "Hidden",
                principal=viewer,
                context=context(viewer),
            )

        cp = update_counterparty(
            db,
            cp.id,
            4,
            cp.name,
            full_name=None,
            principal=admin,
            context=context(admin),
        )
        assert cp.status == "REVIEW" and cp.legal_address == details["legal_address"]
        db.commit()

        events = db.scalars(
            select(HistoryEvent)
            .where(HistoryEvent.object_type == "CP", HistoryEvent.object_id == cp.id)
            .order_by(HistoryEvent.event_id)
        ).all()
        assert len(events) == 5
        assert set(events[1].changes) == {"phone", "version"}
        assert set(events[2].changes) == {"primary_signer_id", "version"}
        assert set(events[3].changes) == {"primary_signer_id", "version"}
        assert set(events[4].changes) == {"full_name", "status", "version"}
        assert db.scalar(
            select(func.count())
            .select_from(HistoryEvent)
            .where(HistoryEvent.object_type == "ORG", HistoryEvent.object_id == org.id)
        ) == 2
    engine.dispose()
