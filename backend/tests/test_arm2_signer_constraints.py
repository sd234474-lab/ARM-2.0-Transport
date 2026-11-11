from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from alembic import command
from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.object_save import SaveContext
from app.arm2.organization_models import Organization
from app.arm2.organization_store import create_organization, update_organization
from app.arm2.representative_models import CounterpartyRepresentative, OrganizationRepresentative
from app.arm2.representative_store import create_representative
from app.config import settings
from app.models import User


@pytest.mark.parametrize("kind", ["CP", "ORG"])
def test_database_rejects_invalid_signer_links_and_preserves_save_history(isolated_arm2_database_url, kind):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    party_model, rep_model, create, update = (
        (Counterparty, CounterpartyRepresentative, create_counterparty, update_counterparty)
        if kind == "CP" else (Organization, OrganizationRepresentative, create_organization, update_organization)
    )
    with Session(engine) as db:
        admin = User(login="constraint-admin", name="Admin", role="ADMIN_OPERATOR",
                     password_hash="unused", active=True, permissions=[])
        db.add(admin)
        db.flush()
        context = SaveContext("HUMAN", str(admin.id), "constraint-test")
        first = create(db, "First", principal=admin, context=context)
        second = create(db, "Second", principal=admin, context=context)
        own = create_representative(db, kind, first.id, "Own", principal=admin, context=context)
        foreign = create_representative(db, kind, second.id, "Foreign", principal=admin, context=context)
        first = update(db, first.id, first.version, first.name, primary_signer_id=own.id,
                       principal=admin, context=context)
        db.commit()
        party_id, other_id, own_id, foreign_id = first.id, second.id, own.id, foreign.id
        count = db.scalar(select(func.count()).select_from(HistoryEvent))
        admin_id = admin.id
    party_table, rep_table = party_model.__tablename__, rep_model.__tablename__
    statements = [
        (f"UPDATE {party_table} SET primary_signer_id=:value, version=version+1 WHERE id=:id",
         {"value": f"{kind}REP-999999", "id": party_id}),
        (f"UPDATE {party_table} SET primary_signer_id=:value, version=version+1 WHERE id=:id",
         {"value": foreign_id, "id": party_id}),
        (f"UPDATE {rep_table} SET parent_id=:value WHERE id=:id", {"value": other_id, "id": own_id}),
        (f"DELETE FROM {rep_table} WHERE id=:id", {"id": own_id}),
    ]
    for sql, params in statements:
        with pytest.raises(IntegrityError) as error, engine.begin() as connection:
            connection.execute(text(sql), params)
        assert error.value.orig.diag.constraint_name == f"fk_arm2_{kind.lower()}_primary_signer_parent"
        with Session(engine) as db:
            assert (db.get(party_model, party_id).primary_signer_id,
                    db.get(party_model, party_id).version) == (own_id, 2)
            assert db.get(rep_model, own_id).parent_id == party_id
            assert db.scalar(select(func.count()).select_from(HistoryEvent)) == count
    # Also bypass adapters using ORM; the same DB constraint must reject this.
    with Session(engine) as db:
        db.get(party_model, party_id).primary_signer_id = foreign_id
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        admin = db.get(User, admin_id)
        result = update(db, party_id, 2, "First", primary_signer_id=None, principal=admin, context=context)
        db.commit()
        db.expire_all()
        assert (result.primary_signer_id, result.version) == (None, 3)
        event = db.scalar(select(HistoryEvent).where(
            HistoryEvent.object_type == kind, HistoryEvent.object_id == party_id,
            HistoryEvent.version_after == 3,
        ))
        assert event.changes["primary_signer_id"] == {"before": own_id, "after": None}
    engine.dispose()


def test_signer_migration_existing_data_diagnostics_and_roundtrip(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", isolated_arm2_database_url)
    settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)
    try:
        command.downgrade(config, "0060")
        with engine.begin() as connection:
            for party, rep, prefix in [
                ("arm2_counterparties", "nsi_counterparty_representatives", "CP"),
                ("arm2_organizations", "nsi_organization_representatives", "ORG"),
            ]:
                connection.execute(text(f"INSERT INTO {party}(id,name,status) VALUES "
                                        f"('{prefix}-000001','Existing','REVIEW'),"
                                        f"('{prefix}-000002','Other','REVIEW')"))
                connection.execute(text(f"INSERT INTO {rep}(id,parent_id,name) VALUES "
                                        f"('{prefix}REP-000001','{prefix}-000001','Existing signer')"))
                connection.execute(text(f"UPDATE {party} SET primary_signer_id='{prefix}REP-000001' "
                                        f"WHERE id='{prefix}-000001'"))
        command.upgrade(config, "head")
        for table in ("arm2_counterparties", "arm2_organizations"):
            fk = next(item for item in inspect(engine).get_foreign_keys(table)
                      if item["constrained_columns"] == ["primary_signer_id", "id"])
            assert fk["referred_columns"] == ["id", "parent_id"]
            assert fk["options"]["ondelete"] == "RESTRICT"
        command.downgrade(config, "0060")
        # Both orphan and wrong-parent links are diagnosed before ANY constraint
        # is installed. The migration must not repair/delete existing records.
        for table, prefix, signer in [
            ("arm2_counterparties", "CP", "CPREP-999999"),
            ("arm2_organizations", "ORG", "ORGREP-000001"),
        ]:
            with engine.begin() as connection:
                connection.execute(text(f"UPDATE {table} SET primary_signer_id=:signer WHERE id=:id"),
                                   {"signer": signer, "id": f"{prefix}-000002"})
            with pytest.raises(RuntimeError, match=f"AUD-02: orphan/mismatched primary signer in {table}"):
                command.upgrade(config, "head")
            with engine.connect() as connection:
                assert connection.scalar(text(f"SELECT primary_signer_id FROM {table} WHERE id=:id"),
                                         {"id": f"{prefix}-000002"}) == signer
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0060"
            # Explicit test-fixture correction, never performed by the migration.
            with engine.begin() as connection:
                connection.execute(text(f"UPDATE {table} SET primary_signer_id=NULL WHERE id=:id"),
                                   {"id": f"{prefix}-000002"})
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT primary_signer_id FROM arm2_counterparties "
                                          "WHERE id='CP-000001'")) == "CPREP-000001"
            assert connection.scalar(text("SELECT primary_signer_id FROM arm2_organizations "
                                          "WHERE id='ORG-000001'")) == "ORGREP-000001"
    finally:
        engine.dispose()
        settings.cache_clear()
