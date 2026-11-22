from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.config import settings


def config():
    return Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))


def test_0070_one_head_clean_schema_and_no_inferred_type(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', isolated_arm2_database_url)
    settings.cache_clear()
    scripts = ScriptDirectory.from_config(config())
    assert scripts.get_heads() == ['0072']
    assert scripts.get_revision('0070').down_revision == '0069'
    sql = create_engine(isolated_arm2_database_url)
    with sql.connect() as db:
        columns = {c['name']: c for c in inspect(db).get_columns('arm2_counterparties')}
        assert not columns['type']['nullable'] and 'status' not in columns and 'is_entrepreneur' not in columns
        assert 'status' not in {c['name'] for c in inspect(db).get_columns('arm2_organizations')}
        assert 'status' in {c['name'] for c in inspect(db).get_columns('arm2_vehicle_types')}
        assert any(f['name'] == 'fk_arm2_cp_primary_signer_parent' for f in inspect(db).get_foreign_keys('arm2_counterparties'))
    command.downgrade(config(), '0069')
    with sql.begin() as db:
        db.execute(text("INSERT INTO arm2_counterparties(id,name,status,version) VALUES ('CP-700001','Must not infer','REVIEW',7)"))
    with pytest.raises(RuntimeError, match='empty CP'):
        command.upgrade(config(), '0070')
    with sql.connect() as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0069'
        assert db.execute(text('SELECT id,name,version,status FROM arm2_counterparties')).one() == ('CP-700001', 'Must not infer', 7, 'REVIEW')
        assert 'type' not in {c['name'] for c in inspect(db).get_columns('arm2_counterparties')}
    sql.dispose()
