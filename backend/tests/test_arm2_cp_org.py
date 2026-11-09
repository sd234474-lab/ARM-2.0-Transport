import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.object_save import ObjectAuthorizationError, SaveContext
from app.arm2.organization_store import create_organization, update_organization
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
