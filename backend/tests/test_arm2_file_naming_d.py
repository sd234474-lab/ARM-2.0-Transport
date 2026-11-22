"""Issue #87: current emitted names, immutable originals and historical packages."""
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from sqlalchemy import select, text
from test_arm2_calibration_approval import ok
from test_arm2_document_api import api as api
from test_arm2_document_file_api import accept, bundle, pdf, stage
from test_arm2_document_file_api import qa as qa
from test_arm2_questionnaire_workflow import apply, choose, exported, transport, upload, workbook
from test_arm2_questionnaire_workflow import b5 as b5

from app.arm2 import document_file_storage as files
from app.arm2.document_kind_models import DocumentKind


def name(response):
    return unquote(response.headers['content-disposition'].split("filename*=utf-8''")[-1])


def setup_case(qa):
    c, db, user, parents, _, disk = qa
    kind = ok(c.post('/api/arm2/document-kinds', json={'name': 'Тарировка', 'applicable_object_types': ['VEH'],
        'max_current_documents': None, 'file_naming_template': 'VEH_CALIBRATION'}), 201)
    type_id = ok(c.post('/api/arm2/types', json={'name': 'Тип без привязки к имени машины',
        'plate_category': 'Автомобильный', 'requires_sts': True, 'requires_calibration': True,
        'requires_wialon': False}), 201)['id']
    vehicle = ok(c.post('/api/arm2/vehicles', json={'name': 'Свободное рабочее имя', 'plate_number': 'E679PH186',
        'type_id': type_id, 'owner_cp_id': parents['cp']}), 201)
    path = f"/api/arm2/veh/{vehicle['id']}/documents"
    doc = ok(c.post(path, json={'kind_ref': kind['id'], 'name': 'Акт проверки'}), 201)
    path += '/' + doc['id']
    ok(accept(c, path, 1, [stage(c, path, pdf('first'), 'Original one.pdf'), stage(c, path, pdf('second'), 'Original two.pdf')]))
    return vehicle['id'], path, kind['id']


def confirm(c, vehicle_id, path, volume):
    vehicle = ok(c.get('/api/arm2/vehicles/' + vehicle_id))
    doc = ok(c.get(path))
    return ok(c.post(f'/api/arm2/veh/{vehicle_id}/calibration/confirm', json={'command_id': str(uuid.uuid4()),
        'vehicle_version': vehicle['version'], 'document_id': doc['id'], 'document_version': doc['version'],
        'revision_id': bundle(c, path)['current_revision_id'], 'plate_number': vehicle['plate_number'], 'volume_m3': volume}))


def file_snapshot(db):
    db.rollback()
    return db.execute(text('SELECT row_to_json(f)::text FROM arm2_document_files f ORDER BY id')).scalars().all()


def test_names_before_after_confirm_only_emitted_name_changes_and_b5_roundtrip(b5):
    c, db, user, parents, _, disk = b5
    vehicle_id, path, kind_id = setup_case(b5)
    before = bundle(c, path)
    revision = before['revisions'][0]
    source = file_snapshot(db)
    for file in revision['files']:
        assert 'НЕТ ДАННЫХ' in file['stored_name'] and 'Е679РН186' in file['stored_name']
        response = c.get(path + f"/revisions/{revision['id']}/files/{file['id']}/download")
        assert response.status_code == 200
        assert name(response) == file['download_name'] == file['stored_name']
    confirm(c, vehicle_id, path, 25.0)
    after = bundle(c, path)
    assert after['document_version'] == before['document_version'] and after['current_revision_id'] == before['current_revision_id']
    assert file_snapshot(db) == source
    for file in after['revisions'][0]['files']:
        emitted = file['download_name']
        assert 'Тарировка' in emitted and 'Е679РН186' in emitted and '25м3' in emitted and file['id'] in emitted
        for action in ['preview', 'download']:
            response = c.get(path + f"/revisions/{revision['id']}/files/{file['id']}/{action}")
            assert response.status_code == 200 and name(response) == emitted
            assert hashlib.sha256(response.content).hexdigest() == file['sha256']
    archive = exported(c, parents['cp'])
    with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
        names = [n for n in zip_file.namelist() if n.startswith('files/')]
        assert len(names) == 2 and len(set(names)) == 2 and all('25м3' in n for n in names)
    draft = choose(c, upload(c, archive, True), cp_target=parents['cp'])
    assert all(d.get('equal') for d in draft['comparison']['documents']), json.dumps(draft['comparison'], ensure_ascii=False)
    receipt, _ = apply(c, draft)
    assert receipt['no_op'] and len(bundle(c, path)['revisions']) == 1
    confirm(c, vehicle_id, path, 27.5)
    assert file_snapshot(db) == source
    current = bundle(c, path)['revisions'][0]['files']
    assert all('27_5м3' in file['download_name'] and 'НЕТ ДАННЫХ' in file['stored_name'] for file in current)
    # Working name is independent of plate and volume, and changing it does not rewrite a FILE.
    record = ok(c.get('/api/arm2/vehicles/' + vehicle_id))
    ok(c.put('/api/arm2/vehicles/' + vehicle_id, json={**{k: record[k] for k in
        ['name','plate_number','type_id','accounting_state','model','wialon_unit_id']},
        'name': 'Совершенно другое имя', 'expected_version': record['version']}))
    assert all('27_5м3' in f['download_name'] and 'Е679РН186' in f['download_name'] for f in bundle(c, path)['revisions'][0]['files'])
    assert file_snapshot(db) == source
    # The already issued B5 ZIP remains an immutable byte sequence with its 25 m3 names.
    with zipfile.ZipFile(io.BytesIO(archive)) as old:
        assert all('25м3' in n for n in old.namelist() if n.startswith('files/'))


def test_new_approval_package_uses_common_names_old_package_is_immutable(qa):
    c, db, *_ = qa
    vehicle_id, path, _ = setup_case(qa)
    confirm(c, vehicle_id, path, 25.0)
    vehicle = ok(c.get('/api/arm2/vehicles/' + vehicle_id))
    request_id = ok(c.post('/api/arm2/approvals/letter', json={'command_id': str(uuid.uuid4()),
        'contract_number_1': 'QA-1', 'contract_number_2': 'QA-2',
        'vehicles': [{'vehicle_id': vehicle_id, 'vehicle_version': vehicle['version']}]}), 201)['request_id']
    request = ok(c.get('/api/arm2/approvals/' + request_id))
    url = f"/api/arm2/approvals/{request_id}/files/{request['package_file_id']}/download"
    original = c.get(url).content
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        assert len([n for n in archive.namelist() if '25м3' in n]) == 2
    confirm(c, vehicle_id, path, 27.0)
    assert c.get(url).content == original
    assert ok(c.get('/api/arm2/approvals/' + request_id)) == request


def test_b5_explicit_existing_vehicle_binding_without_row_uses_same_plate_and_missing_volume(b5):
    c, db, user, parents, *_ = b5
    vehicle_id, path, kind_id = setup_case(b5)
    confirm(c, vehicle_id, path, 25)
    original = bundle(c, path)
    draft = upload(c, transport(workbook(), {'new.pdf':pdf('new file from questionnaire')}), True)
    attachment = draft['comparison']['documents'][0]
    draft = choose(c, draft, cp_target=parents['cp'], documents={attachment['key']:{
        'parent_key':vehicle_id, 'kind_id':kind_id, 'document_id':path.split('/')[-1], 'append':True, 'accept':True}})
    receipt, _ = apply(c, draft)
    assert not receipt['no_op']
    after = bundle(c, path)
    previous_ids = {f['id'] for f in original['revisions'][0]['files']}
    new = [f for f in after['revisions'][0]['files'] if f['id'] not in previous_ids]
    assert len(new) == 1
    assert 'Е679РН186' in new[0]['stored_name'] and 'НЕТ ДАННЫХ' in new[0]['stored_name']
    assert all('НЕТ ДАННЫХ' in f['download_name'] for f in after['revisions'][0]['files'])


@pytest.mark.parametrize('parents,label', [(['CP'], 'Тарировка'), (['ORG'], 'Тарировка'),
    (['CP','VEH'], 'Тарировка'), (['VEH'], 'СТС')])
def test_incompatible_kind_is_rejected_on_create_and_update(qa, parents, label):
    c, db, *_ = qa
    body = {'name': label, 'applicable_object_types': parents, 'max_current_documents': None,
            'file_naming_template': 'VEH_CALIBRATION'}
    assert c.post('/api/arm2/document-kinds', json=body).status_code == 422
    vehicle_id, path, kind_id = setup_case(qa)
    kind = ok(c.get('/api/arm2/document-kinds/' + kind_id))
    assert c.put('/api/arm2/document-kinds/' + kind_id, json={**body, 'expected_version': kind['version']}).status_code == 422
    assert ok(c.get('/api/arm2/document-kinds/' + kind_id)) == kind


def test_absent_and_revoked_rights_do_not_expose_download_or_configuration(qa):
    c, db, user, parents, _, disk = qa
    vehicle_id, path, kind_id = setup_case(qa)
    before = bundle(c, path)['revisions'][0]
    user.role, user.permissions = 'OPERATOR', []
    db.commit()
    assert c.get(path + f"/revisions/{before['id']}/files/{before['files'][0]['id']}/download").status_code == 403
    assert c.put('/api/arm2/document-kinds/' + kind_id, json={'name':'Тарировка', 'applicable_object_types':['VEH'],
        'max_current_documents':None, 'file_naming_template':'VEH_CALIBRATION', 'expected_version':1}).status_code == 403


@pytest.mark.parametrize('preset,document,parent,kind,expected', [
    ('DOCUMENT_NAME', '', 'Parent', 'Kind', 'НЕТ ДАННЫХ'),
    ('PARENT_AND_KIND', 'Doc', None, 'Kind', 'НЕТ ДАННЫХ Kind'),
    ('PARENT_AND_KIND', 'Doc', 'Parent', '', 'Parent НЕТ ДАННЫХ'),
    ('DOCUMENT_NAME', 'Original A_', '', '', 'Original A'),
    ('PARENT_AND_KIND', '', 'A_', '_B', 'A_ _B'),
])
def test_old_presets_and_general_missing_parameters(preset, document, parent, kind, expected):
    result = files.safe_name(preset, SimpleNamespace(name=document), SimpleNamespace(name=parent),
        SimpleNamespace(name=kind), 'FILE-000001', 'pdf')
    assert result == expected + '-FILE-000001.pdf'


@pytest.mark.parametrize('change', [
    {'calibration_m3': None}, {'calibration_m3': 0}, {'calibration_m3': -1},
    {'calibration_m3': float('nan')}, {'calibration_m3': float('inf')},
    {'calibration_confirmed_at': None}, {'calibration_confirmed_by': None},
    {'calibration_document_id': 'another'}, {'calibration_revision_id': 'old'},
    {'calibration_plate_number': 'another'},
])
def test_no_positive_volume_from_absent_invalid_or_stale_proof(change):
    parent = SimpleNamespace(**({'plate_number':'Е679РН186', 'calibration_m3':25.0,
        'calibration_confirmed_at':'synthetic', 'calibration_confirmed_by':1,
        'calibration_document_id':'doc', 'calibration_revision_id':'rev',
        'calibration_plate_number':'Е679РН186'} | change))
    doc = SimpleNamespace(id='doc', parent_type='VEH', current_revision_id='rev')
    kind = SimpleNamespace(name='Тарировка', applicable_object_types=['VEH'])
    emitted = files.safe_name('VEH_CALIBRATION', doc, parent, kind, 'FILE-000001', 'pdf')
    assert 'НЕТ ДАННЫХ' in emitted and '25м3' not in emitted


def test_new_revision_does_not_inherit_old_confirmation_and_preserves_old_file(qa):
    c, db, *_ = qa
    vehicle_id, path, _ = setup_case(qa)
    confirm(c, vehicle_id, path, 25)
    old = bundle(c, path)
    original = old['revisions'][0]['files'][0]
    old_url = path + f"/revisions/{old['current_revision_id']}/files/{original['id']}/download"
    old_bytes = c.get(old_url).content
    candidate = stage(c, path, pdf('replacement'), 'New original.pdf')
    ok(accept(c, path, old['document_version'], [candidate]))
    after = bundle(c, path)
    assert after['current_revision_id'] != old['current_revision_id']
    assert all('НЕТ ДАННЫХ' in f['stored_name'] and 'НЕТ ДАННЫХ' in f['download_name']
               for r in after['revisions'] for f in r['files'])
    assert c.get(old_url).content == old_bytes
    confirm(c, vehicle_id, path, 27)
    assert 'НЕТ ДАННЫХ' in name(c.get(old_url))
    assert '27м3' in bundle(c, path)['revisions'][0]['files'][0]['download_name']


def test_migration_0072_to_0073_preserves_data_and_guards_naming_history(qa):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    c, db, *_ = qa
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    assert ScriptDirectory.from_config(config).get_heads() == ['0073']
    before = db.execute(select(DocumentKind.id, DocumentKind.name, DocumentKind.version, DocumentKind.file_naming_template)).all()
    db.rollback()
    command.downgrade(config, '0072')
    command.upgrade(config, '0073')
    assert db.execute(select(DocumentKind.id, DocumentKind.name, DocumentKind.version, DocumentKind.file_naming_template)).all() == before
    db.rollback()
    _, _, kind_id = setup_case(qa)
    before_files = file_snapshot(db)
    db.rollback()
    with pytest.raises(RuntimeError, match='0073 downgrade refused'):
        command.downgrade(config, '0072')
    assert file_snapshot(db) == before_files
    assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0073'


def test_real_restore_includes_new_preset_and_confirmed_vehicle_proof(qa, monkeypatch, tmp_path):
    import test_arm2_approval_restore as restore
    original = restore.prepare
    def configured(environment):
        result = original(environment)
        c, *_ = environment
        document = ok(c.get(result[0][1]))
        kind = ok(c.get('/api/arm2/document-kinds/' + document['kind_ref']))
        ok(c.put('/api/arm2/document-kinds/' + kind['id'], json={**{k:kind[k] for k in
            ['name','applicable_object_types','max_current_documents']}, 'file_naming_template':'VEH_CALIBRATION',
            'expected_version':kind['version']}))
        return result
    monkeypatch.setattr(restore, 'prepare', configured)
    restore.test_sql_and_file_runtime_restore_reopens_evidence_and_resumes_private_reply(qa, monkeypatch, tmp_path)
