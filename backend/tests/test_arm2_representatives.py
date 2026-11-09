import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.object_save import SaveContext
from app.arm2.organization_store import create_organization
from app.arm2.representative_store import create_representative, update_representative
from app.models import User


def ctx(user): return SaveContext("HUMAN", str(user.id), "representative-test")


def test_representatives_parent_signer_state_and_user_link(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    with Session(engine) as db:
        admin = User(login="rep-admin", name="rep-admin", password_hash="unused", role="ADMIN_OPERATOR", active=True, permissions=[])
        account = User(login="linked", name="linked", password_hash="unused", role="OPERATOR", active=True, permissions=[])
        db.add_all([admin, account])
        db.flush()
        cp1 = create_counterparty(db, "CP1", principal=admin, context=ctx(admin))
        cp2 = create_counterparty(db, "CP2", principal=admin, context=ctx(admin))
        org = create_organization(db, "ORG", principal=admin, context=ctx(admin))
        cprep = create_representative(db, "CP", cp1.id, "Signer", user_id=account.id, principal=admin, context=ctx(admin))
        orgrep = create_representative(db, "ORG", org.id, "Org signer", user_id=account.id, principal=admin, context=ctx(admin))
        assert (cprep.id, orgrep.id) == ("CPREP-000001", "ORGREP-000001")
        cp1 = update_counterparty(db, cp1.id, 1, cp1.name, primary_signer_id=cprep.id, principal=admin, context=ctx(admin))
        assert cp1.primary_signer_id == cprep.id
        with pytest.raises(ValueError, match="same card"):
            update_counterparty(db, cp2.id, 1, cp2.name, primary_signer_id=cprep.id, principal=admin, context=ctx(admin))
        cprep = update_representative(db, "CP", cprep.id, 1, cp1.id, cprep.name, state="Архивный", user_id=None,
                                      principal=admin, context=ctx(admin))
        assert (cprep.state, cprep.user_id) == ("Архивный", None)
        account.active = False
        db.flush()
        cprep = update_representative(db, "CP", cprep.id, 2, cp1.id, cprep.name, state="Действующий", user_id=account.id,
                                      principal=admin, context=ctx(admin))
        assert cprep.user_id == account.id
        with pytest.raises(ValueError, match="does not exist"):
            create_representative(db, "CP", cp1.id, "Bad", user_id=999999, principal=admin, context=ctx(admin))
        db.rollback()
    engine.dispose()
