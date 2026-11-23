import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from test_arm2_questionnaire_workflow import api as api
from test_arm2_questionnaire_workflow import b5 as b5
from test_arm2_questionnaire_workflow import qa as qa
from test_arm2_questionnaire_workflow import snapshot

from alembic import command
from app.arm2.history_models import HistoryEvent
from app.arm2.questionnaire_models import (
    QuestionnaireDraft,
    QuestionnaireExport,
    QuestionnaireReceipt,
    QuestionnaireUpload,
)
from app.config import settings


def config():
    return Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))


def test_0069_upgrade_preserves_0068_rows_ids_versions_history_and_sequence(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', isolated_arm2_database_url)
    settings.cache_clear()
    scripts = ScriptDirectory.from_config(config())
    assert scripts.get_heads() == ['0074'] and scripts.get_revision('0069').down_revision == '0068'
    command.downgrade(config(), '0068')
    engine = create_engine(isolated_arm2_database_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO arm2_counterparties(id,name,inn,version,status) VALUES('CP-700001','Frozen CP','0000000000',7,'REVIEW')"))
        db.execute(text("INSERT INTO arm2_vehicles(id,name,plate_number,owner_cp_id,version,accounting_state) VALUES('VEH-700001','Frozen VEH','А700АА86','CP-700001',4,'Действующая')"))
        db.execute(text("""INSERT INTO arm2_history_events(object_type,object_id,operation,actor_kind,actor_id,source,version_before,version_after,changes)
            VALUES('CP','CP-700001','UPDATED','HUMAN','1','frozen-0068',6,7,CAST(:changes AS jsonb))"""),
            {'changes': json.dumps({'name': {'before': 'Earlier frozen name', 'after': 'Frozen CP'}})})
    with engine.connect() as db:
        old_cp = db.execute(text('SELECT row_to_json(t)::jsonb FROM arm2_counterparties t')).scalar_one()
        history = db.execute(text('SELECT row_to_json(t)::text FROM arm2_history_events t')).scalars().all()
        sequence = db.execute(text('SELECT last_value,is_called FROM arm2_cp_id_seq')).one()
    command.upgrade(config(), '0069')
    with engine.connect() as db:
        current = db.execute(text('SELECT row_to_json(t)::jsonb FROM arm2_counterparties t')).scalar_one()
        assert {k: v for k, v in current.items() if k not in ('last_name', 'first_name', 'middle_name')} == old_cp
        assert all(current[f] is None for f in ('last_name', 'first_name', 'middle_name'))
        assert db.execute(text('SELECT row_to_json(t)::text FROM arm2_history_events t')).scalars().all() == history
        assert db.execute(text('SELECT owner_cp_id,version FROM arm2_vehicles')).one() == ('CP-700001', 4)
        assert db.execute(text('SELECT last_value,is_called FROM arm2_cp_id_seq')).one() == sequence
    engine.dispose()


@pytest.mark.parametrize('reason', ['name', 'history', 'draft', 'upload', 'receipt', 'export'])
def test_0069_guard_never_discards_names_history_or_questionnaire_ledgers(b5, reason):
    c, db, user, parents, *_ = b5
    if reason == 'name':
        db.execute(text("UPDATE arm2_counterparties SET last_name='Keep normalized name' WHERE id=:id"), {'id': parents['cp']})
    elif reason == 'history':
        db.add(HistoryEvent(object_type='CP', object_id=parents['cp'], operation='UPDATED', actor_kind='HUMAN', actor_id=str(user.id),
            source='migration-test', version_before=1, version_after=2, changes={'last_name': {'before': 'Old name', 'after': None}}, related_objects=[]))
    else:
        draft = QuestionnaireDraft(id=str(uuid4()), author_id=user.id, status='OPEN', revision=1, filename='synthetic.xlsx',
            source_sha256='a'*64, payload={}, choices={}, preview={}, staging={})
        db.add(draft)
        db.flush()
        if reason == 'upload':
            db.add(QuestionnaireUpload(id='b'*64, draft_id=draft.id, name='file.pdf', size=10, sha256='c'*64,
                state='STAGED', prepared={}))
        if reason == 'receipt':
            db.add(QuestionnaireReceipt(command_id=str(uuid4()), draft_id=draft.id, author_id=user.id, fingerprint='d'*64, result={}))
        if reason == 'export':
            db.add(QuestionnaireExport(id=str(uuid4()), author_id=user.id, cp_id=parents['cp'], missing=[]))
    db.commit()
    before = snapshot(db)
    db.commit()
    with pytest.raises(RuntimeError, match='0070.*downgrade refused'):
        command.downgrade(config(), '0068')
    assert snapshot(db) == before
    assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0074'


def test_0071_preserves_existing_files_and_enables_only_opaque_bin(isolated_arm2_database_url, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', isolated_arm2_database_url)
    settings.cache_clear()
    assert ScriptDirectory.from_config(config()).get_revision('0071').down_revision == '0070'
    command.downgrade(config(), '0070')
    engine = create_engine(isolated_arm2_database_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO arm2_counterparties(id,name,type,version) VALUES('CP-700001','Synthetic migration','Юридическое лицо',3)"))
        db.execute(text("INSERT INTO arm2_document_kinds(id,name,applicable_object_types) VALUES('DOC-KIND-700001','Synthetic','[\"CP\"]'::jsonb)"))
        db.execute(text("INSERT INTO arm2_documents(id,parent_type,parent_id,kind_ref,name,version) VALUES('DOCUMENT-700001','CP','CP-700001','DOC-KIND-700001','Synthetic',2)"))
        for i, extension in enumerate(('pdf', 'png', 'jpg', 'gif', 'webp', 'docx', 'xlsx', 'pptx'), 1):
            db.execute(text("""INSERT INTO arm2_document_files(id,document_id,original_upload_name,stored_name,storage_ref,size,sha256,media_type,extension)
                VALUES(:id,'DOCUMENT-700001',:name,:name,:ref,10,:sha,'synthetic/metadata',:ext)"""),
                {'id': f'FILE-{700000+i}', 'name': 'synthetic.'+extension, 'ref': 'disk:/SYNTHETIC/'+extension,
                 'sha': f'{i:064x}', 'ext': extension})
    with engine.connect() as db:
        before = db.execute(text('SELECT row_to_json(t)::text FROM arm2_document_files t ORDER BY id')).scalars().all()
        old_constraint = next(c['sqltext'] for c in inspect(db).get_check_constraints('arm2_document_files') if c['name'] == 'ck_arm2_file_metadata')
        assert 'bin' not in old_constraint
    command.upgrade(config(), '0071')
    with engine.connect() as db:
        assert db.execute(text('SELECT row_to_json(t)::text FROM arm2_document_files t ORDER BY id')).scalars().all() == before
    with engine.begin() as db:
        db.execute(text("""INSERT INTO arm2_document_files(id,document_id,original_upload_name,stored_name,storage_ref,size,sha256,media_type,extension)
            VALUES('FILE-799999','DOCUMENT-700001','card.doc','synthetic.bin','disk:/SYNTHETIC/bin',12,repeat('f',64),'application/octet-stream','bin')"""))
    with pytest.raises(RuntimeError, match='0071 downgrade refused'):
        command.downgrade(config(), '0070')
    with engine.connect() as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0071'
        assert db.scalar(text("SELECT original_upload_name FROM arm2_document_files WHERE extension='bin'")) == 'card.doc'
    with pytest.raises(IntegrityError, match='ck_arm2_file_metadata'), engine.begin() as db:
        db.execute(text("""INSERT INTO arm2_document_files(id,document_id,original_upload_name,stored_name,storage_ref,size,sha256,media_type,extension)
            VALUES('FILE-799998','DOCUMENT-700001','bad.exe','bad.exe','disk:/SYNTHETIC/bad',12,repeat('e',64),'application/octet-stream','exe')"""))
    engine.dispose()
