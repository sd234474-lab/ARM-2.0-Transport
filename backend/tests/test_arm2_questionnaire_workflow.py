"""T01–T33 AUTHOR-RUN: native auth/API -> SQL/FILE/HIST -> reopen; synthetic only."""
import base64
import hashlib
import io
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_arm2_document_file_api import accept, bundle, create, pdf, stage
from test_arm2_document_file_api import api as api
from test_arm2_document_file_api import qa as qa
from test_arm2_vehicle_links import command, current

from app.arm2.counterparty_models import Counterparty
from app.arm2.document_file_models import DocumentFile
from app.arm2.document_models import Document
from app.arm2.history_models import HistoryEvent
from app.arm2.questionnaire_models import (
    QuestionnaireDraft,
    QuestionnaireExport,
    QuestionnaireReceipt,
    QuestionnaireUpload,
)
from app.arm2.questionnaire_parse import MANIFEST, PASSPORT, VEHICLES
from app.arm2.vehicle_models import Vehicle
from app.auth import hasher
from app.models import User


def workbook(cp=None, rows=None, extra=None):
    book = Workbook()
    passport = book.active
    passport.title = PASSPORT
    passport['B3'] = 'ARM_IMPORT_V2'
    for key, value in (cp or {}).items():
        passport[key] = value
    sheet = book.create_sheet(VEHICLES)
    for index, row in enumerate(rows or [], 5):
        for col, value in row.items():
            sheet[f'{col}{index}'] = value
    book.create_sheet('Инструкция')['A1'] = 'Synthetic QA only'
    book.create_sheet('Справочники')['A1'] = 'No arbitrary details'
    if extra:
        extra(book)
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def transport(book, attachments):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('questionnaire.xlsx', book)
        for name, data in attachments.items():
            archive.writestr(name, data)
    return output.getvalue()


def check(response, status=200):
    assert response.status_code == status, response.text[:1000]
    return response.json()


@pytest.fixture
def b5(qa, monkeypatch):
    monkeypatch.setenv('ARM2_TABLE_SIGNING_KEY', 'synthetic-b5-signing-key-no-live-secret-2026')
    from app.config import settings
    settings.cache_clear()
    yield qa


def upload(c, content, zip=False):
    return check(c.post('/api/arm2/questionnaires', params={'filename': 'synthetic.zip' if zip else 'synthetic.xlsx'}, content=content), 201)


def choose(c, draft, **choices):
    return check(c.put('/api/arm2/questionnaires/' + draft['id'], json={'revision': draft['revision'],
        'choices': {**draft.get('choices', {}), **choices}}))


def apply(c, draft, status=200, body=None):
    body = body or {'command_id': str(uuid4()), 'revision': draft['revision'], 'preview_id': draft['preview_id']}
    result = check(c.post('/api/arm2/questionnaires/' + draft['id'] + '/apply', json=body), status)
    return result, body


def reopen(c, draft):
    return check(c.get('/api/arm2/questionnaires/' + draft['id']))


def count(db, model):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def snapshot(db):
    db.rollback()
    return {table: db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all()
            for table in ('arm2_counterparties', 'nsi_counterparty_representatives', 'arm2_vehicles',
                          'arm2_documents', 'arm2_document_files', 'arm2_document_revisions', 'arm2_history_events')}


def doc_kind(c, name='СТС', parent='VEH'):
    return check(c.post('/api/arm2/document-kinds', json={'name': name, 'applicable_object_types': [parent],
        'max_current_documents': None, 'file_naming_template': 'DOCUMENT_NAME'}), 201)['id']


def file_choices(draft, kind):
    return {d['key']: {'kind_id': kind, 'accept': True} for d in draft['comparison']['documents'] if d.get('upload_id')}


def test_t01_xlsx_no_files_preview_is_private_and_single_cp_field_save(b5):
    c, db, _, parents, *_ = b5
    before = snapshot(db)
    d = upload(c, workbook({'B18': '+7 synthetic'}))
    assert snapshot(db) == before
    d = choose(c, d, cp_target=parents['cp'], cp_fields=['phone'])
    saved, _ = apply(c, d)
    assert len(saved['event_ids']) == 1 and saved['cp_id'] == parents['cp']
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['phone'] == '+7 synthetic'
    assert reopen(c, d)['receipt'] == saved
    assert count(db, QuestionnaireReceipt) == 1


def test_t02_n_p_bind_to_declared_vehicle_not_physical_filename(b5):
    c, db, _, parents, *_ = b5
    calibration = doc_kind(c, 'Тарировка')
    sts = doc_kind(c)
    book = workbook(rows=[{'E': 'A123BC86', 'N': 'not-a-vehicle-name.pdf', 'P': 'random.pdf'}])
    d = upload(c, transport(book, {'not-a-vehicle-name.pdf': pdf('calibration'), 'random.pdf': pdf('sts')}), True)
    d = choose(c, d, cp_target=parents['cp'], documents={'v:5:sts': {'kind_id': sts, 'accept': True},
                'v:5:calibration': {'kind_id': calibration, 'accept': True}})
    saved, _ = apply(c, d)
    assert len(saved['objects']) == 2
    for doc in db.scalars(select(Document)):
        assert doc.parent_type == 'VEH' and doc.parent_id == parents['veh']
    assert count(db, DocumentFile) == 2
    assert reopen(c, d)['receipt'] == saved


def test_t03_unchanged_fields_require_no_acceptance_or_new_history(b5):
    c, db, _, parents, *_ = b5
    before = snapshot(db)
    d = choose(c, upload(c, workbook({'B12': 'CP'})), cp_target=parents['cp'])
    result, _ = apply(c, d)
    assert result['no_op'] and not result['event_ids'] and snapshot(db) == before


def test_t04_lost_response_uuid_fingerprint_replays_receipt_without_duplicates(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'receipt-only'})), cp_target=parents['cp'], cp_fields=['phone'])
    saved, body = apply(c, d)
    before = snapshot(db)
    assert apply(c, d, body=body)[0] == saved and snapshot(db) == before
    apply(c, d, status=409, body={**body, 'revision': body['revision'] + 1})
    assert count(db, QuestionnaireReceipt) == 1


def test_t05_only_selected_field_changes_other_values_and_history_preserved(b5):
    c, db, _, parents, *_ = b5
    original = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    d = choose(c, upload(c, workbook({'B11': 'Not selected', 'B18': 'Selected phone', 'B19': 'not selected invalid'})),
               cp_target=parents['cp'], cp_fields=['phone'])
    result, _ = apply(c, d)
    fresh = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    assert {k: fresh[k] for k in original if k not in ('phone', 'version')} == {k: v for k, v in original.items() if k not in ('phone', 'version')}
    assert fresh['phone'] == 'Selected phone' and fresh['version'] == original['version'] + 1
    event = db.get(HistoryEvent, result['event_ids'][0])
    assert set(event.changes) == {'phone', 'version'}


def test_t06_empty_inn_allowed_duplicate_inn_is_not_a_new_cp(b5):
    c, db, _, parents, *_ = b5
    existing = check(c.post('/api/arm2/counterparties', json={'name': 'Known INN', 'inn': '1234567890'}), 201)
    d = choose(c, upload(c, workbook({'B12': 'Explicit new', 'B13': '1234567890'})), cp_target='new', cp_fields=['name', 'inn'])
    before = count(db, Counterparty)
    apply(c, d, status=422)
    assert count(db, Counterparty) == before
    assert any(r['id'] == existing['id'] for r in reopen(c, d)['comparison']['cp']['candidates'])
    d = choose(c, upload(c, workbook({'B12': 'Empty INN'})), cp_target='new', cp_fields=['name'])
    result, _ = apply(c, d)
    assert check(c.get('/api/arm2/counterparties/' + result['cp_id']))['inn'] is None


def test_t07_legacy_map_formula_hints_no_arbitrary_details_or_zero_guessing(b5):
    c, db, _, parents, *_ = b5
    book = workbook({'A13': 'inn:ИНН', 'B8': 'Synthetic last', 'B9': 'Synthetic first', 'B10': 'Synthetic middle',
        'B18': 'Legacy phone', 'B24': '001234567', 'B25': '00000000000000000001', 'B7': 'ignored'},
        [{'B': 'V3-old', 'E': 'A123BC86', 'G': '=1+1', 'V': 'V3 private hint'}])
    d = choose(c, upload(c, book), cp_target=parents['cp'], cp_fields=['last_name', 'first_name', 'middle_name', 'phone', 'bik', 'settlement_account'])
    saved, _ = apply(c, d)
    cp = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    assert cp['bik'] == '001234567' and cp['settlement_account'] == '00000000000000000001'
    assert cp['last_name'] == 'Synthetic last' and cp['first_name'] == 'Synthetic first'
    assert cp['middle_name'] == 'Synthetic middle' and cp['inn'] is None
    assert 'details' not in cp and 'inn:ИНН' not in str(db.get(HistoryEvent, saved['event_ids'][0]).changes)
    assert current(c, parents['veh'])['model'] is None


def test_t08_identical_representative_names_do_not_merge_or_assign_users(b5):
    c, db, _, parents, *_ = b5
    body = workbook({'B20': 'Synthetic Person', 'B21': 'Director', 'B22': 'Charter'})
    identifiers = []
    for _ in range(2):
        d = choose(c, upload(c, body), cp_target=parents['cp'], representatives={'0': {'target': 'new', 'fields': ['name', 'position', 'authority_basis']}})
        result, _ = apply(c, d)
        identifiers.extend(o['id'] for o in result['objects'] if o['kind'] == 'CPREP')
    assert len(set(identifiers)) == 2
    for identifier in identifiers:
        rep = check(c.get(f"/api/arm2/counterparties/{parents['cp']}/representatives/{identifier}"))
        assert rep['user_id'] is None


def test_t09_valid_invalid_duplicate_plate_rows_are_explicit(b5):
    c, db, _, parents, *_ = b5
    d = upload(c, workbook(rows=[{'I': 'A124BC86', 'J': 'Valid'}, {'I': 'bad'}, {'I': 'A125BC86'}, {'I': 'А125ВС86'}]))
    rows = d['comparison']['vehicles']
    assert not rows[0]['errors'] and all(r['errors'] for r in rows[1:])
    d = choose(c, d, cp_target=parents['cp'], vehicles={'v:5': {'include': True, 'fields': ['name']}})
    apply(c, d)
    assert count(db, Vehicle) == 2


def test_t10_unknown_type_explicit_without_type_and_confirmed_category(b5):
    c, db, _, parents, *_ = b5
    d = upload(c, workbook(rows=[{'I': 'A124BC86', 'H': 'No such type', 'J': 'Without type'}]))
    d = choose(c, d, cp_target=parents['cp'], vehicles={'v:5': {'include': True, 'fields': ['name']}})
    apply(c, d, status=422)
    d = choose(c, d, vehicles={'v:5': {'include': True, 'without_type': True, 'fields': ['name']}})
    apply(c, d)
    assert db.scalar(select(Vehicle.type_id).where(Vehicle.plate_number == 'А124ВС86')) is None
    typ = check(c.post('/api/arm2/types', json={'name': 'Confirmed', 'plate_category': 'Автомобильный',
        'requires_sts': True, 'requires_calibration': False, 'requires_wialon': False}), 201)
    d = choose(c, upload(c, workbook(rows=[{'I': 'A125BC86', 'H': typ['id'], 'J': 'Typed'}])),
        cp_target=parents['cp'], vehicles={'v:5': {'include': True, 'fields': ['name', 'type_id']}})
    apply(c, d)
    assert db.scalar(select(Vehicle.type_id).where(Vehicle.plate_number == 'А125ВС86')) == typ['id']


def test_t11_archive_reactivation_is_an_explicit_field_not_readiness(b5):
    c, db, _, parents, *_ = b5
    v = current(c, parents['veh'])
    check(c.put('/api/arm2/vehicles/' + v['id'], json={k: v[k] for k in ('name', 'plate_number', 'type_id', 'model', 'wialon_unit_id')} |
        {'expected_version': v['version'], 'accounting_state': 'Архивная'}))
    d = choose(c, upload(c, workbook(rows=[{'B': v['id'], 'E': v['plate_number'], 'C': 'ЗАЯВЛЕНО'}])), cp_target=parents['cp'])
    assert d['comparison']['vehicles'][0]['fields']['accounting_state']['proposed'] == 'Действующая'
    apply(c, d)
    assert current(c, v['id'])['accounting_state'] == 'Архивная'
    d = choose(c, upload(c, workbook(rows=[{'B': v['id'], 'E': v['plate_number']}])), cp_target=parents['cp'],
               vehicles={'v:5': {'include': True, 'fields': ['accounting_state']}})
    apply(c, d)
    assert current(c, v['id'])['accounting_state'] == 'Действующая'


def test_t12_owner_reassignment_requires_b2_confirmation(b5):
    c, db, _, parents, *_ = b5
    v = current(c, parents['veh'])
    check(command(c, v, ('ORG', parents['org'])))
    d = choose(c, upload(c, workbook(rows=[{'E': v['plate_number']}])), cp_target=parents['cp'],
               vehicles={'v:5': {'include': True, 'fields': ['owner']}})
    before = snapshot(db)
    apply(c, d, status=409)
    assert snapshot(db) == before
    d = choose(c, d, vehicles={'v:5': {'include': True, 'fields': ['owner'], 'confirm_owner': True}})
    apply(c, d)
    assert current(c, v['id'])['owner_cp_id'] == parents['cp']


def test_t13_withdrawal_only_detaches_current_declarant_never_archives(b5):
    c, db, _, parents, *_ = b5
    v = current(c, parents['veh'])
    check(command(c, v, ('CP', parents['cp'])))
    def withdraw():
        return choose(c, upload(c, workbook(rows=[{'E': v['plate_number'], 'C': 'ВЫБЫЛО'}])), cp_target=parents['cp'],
            vehicles={'v:5': {'include': True, 'fields': ['owner'], 'confirm_owner': True}})
    apply(c, withdraw())
    assert current(c, v['id'])['owner_cp_id'] is None and current(c, v['id'])['accounting_state'] == 'Действующая'
    check(command(c, current(c, v['id']), ('ORG', parents['org'])))
    result, _ = apply(c, withdraw())
    assert result['no_op'] and current(c, v['id'])['owner_org_id'] == parents['org']


def test_t14_missing_ambiguous_file_does_not_guess_parent_or_block_other_fields(b5):
    c, db, _, parents, *_ = b5
    content = transport(workbook({'B18': 'Independent'}, [{'E': 'A123BC86', 'N': 'same.pdf', 'P': 'missing.pdf'}]),
                        {'a/same.pdf': pdf('a'), 'b/same.pdf': pdf('b')})
    d = upload(c, content, True)
    assert all(d['error'] for d in d['comparison']['documents'])
    d = choose(c, d, cp_target=parents['cp'], cp_fields=['phone'])
    apply(c, d)
    assert count(db, DocumentFile) == 0 and check(c.get('/api/arm2/counterparties/' + parents['cp']))['phone'] == 'Independent'


def test_t15_company_card_and_unbound_file_require_selected_parents(b5):
    c, db, _, parents, *_ = b5
    kind = doc_kind(c, 'Company card', 'CP')
    sts = doc_kind(c)
    d = upload(c, transport(workbook({'B28': 'card.pdf'}), {'card.pdf': pdf('company'), 'unknown.pdf': pdf('vehicle')}), True)
    unknown = next(p for p in d['comparison']['documents'] if p['name'] == 'unknown.pdf')
    d = choose(c, d, cp_target=parents['cp'], documents={'company': {'kind_id': kind, 'accept': True},
        unknown['key']: {'parent_key': parents['veh'], 'kind_id': sts, 'accept': True}})
    apply(c, d)
    assert {(d.parent_type, d.parent_id) for d in db.scalars(select(Document))} == {('CP', parents['cp']), ('VEH', parents['veh'])}


def seed_document(b5, multifile=False):
    c, db, _, parents, *_ = b5
    check(command(c, current(c, parents['veh']), ('CP', parents['cp'])))
    url = create(b5, 'veh')
    check(accept(c, url, 1, [stage(c, url, pdf('first'))] + ([stage(c, url, pdf('second'))] if multifile else [])))
    return url


def exported(c, cp, format='zip', reason=None):
    result = check(c.post(f'/api/arm2/questionnaires/exports/{cp}', json={'format': format, **({'incomplete_reason': reason} if reason else {})}))
    return base64.b64decode(result['data']) if 'data' in result else result


def test_t16_signed_document_parent_identity_and_sha_return_is_noop(b5):
    c, db, _, parents, *_ = b5
    url = seed_document(b5)
    archive = exported(c, parents['cp'])
    d = choose(c, upload(c, archive, True), cp_target=parents['cp'])
    assert all(p['equal'] for p in d['comparison']['documents'])
    before = snapshot(db)
    result, _ = apply(c, d)
    assert result['no_op'] and snapshot(db) == before and len(bundle(c, url)['revisions']) == 1


def test_t17_changed_file_new_revision_preserves_other_files_and_old_bytes(b5):
    c, db, _, parents, *_ = b5
    url = seed_document(b5, True)
    before = bundle(c, url)
    archive = exported(c, parents['cp'])
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        members = {n: source.read(n) for n in source.namelist()}
    target = next(n for n in members if 'FILE-000001.' in n)
    members[target] = pdf('changed original')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, data in members.items():
            z.writestr(name, data)
    d = choose(c, upload(c, out.getvalue(), True), cp_target=parents['cp'])
    changed = next(p for p in d['comparison']['documents'] if p['name'] == target)
    assert changed['document_id'] == url.split('/')[-1] and not changed['equal']
    d = choose(c, d, documents={changed['key']: {'accept': True}})
    saved, _ = apply(c, d)
    after = bundle(c, url)
    assert len(after['revisions']) == 2 and after['revisions'][1] == before['revisions'][0]
    assert after['revisions'][0]['files'][1]['id'] == before['revisions'][0]['files'][1]['id']
    old = before['revisions'][0]
    assert c.get(url + f"/revisions/{old['id']}/files/{old['files'][0]['id']}/download").content == pdf('first')
    assert reopen(c, d)['receipt'] == saved


def test_t18_missing_attachment_never_removes_accepted_original(b5):
    c, db, _, parents, *_ = b5
    url = seed_document(b5)
    before = bundle(c, url)
    d = choose(c, upload(c, workbook(rows=[{'E': 'A123BC86', 'P': 'absent.pdf'}])), cp_target=parents['cp'])
    apply(c, d)
    assert bundle(c, url) == before


def test_t19_volume_is_review_only_not_a_vehicle_write(b5):
    c, db, _, parents, *_ = b5
    before = current(c, parents['veh'])
    d = choose(c, upload(c, workbook(rows=[{'E': 'A123BC86', 'L': '37.5'}])), cp_target=parents['cp'], vehicles={'v:5': {'include': True}})
    assert d['comparison']['vehicles'][0]['volume_proposal'] == '37.5'
    saved, _ = apply(c, d)
    assert saved['no_op'] and current(c, parents['veh']) == before


def test_t20_file_acceptance_does_not_invent_or_clear_vehicle_approval(b5):
    c, db, _, parents, *_ = b5
    before = current(c, parents['veh'])
    kind = doc_kind(c)
    d = upload(c, transport(workbook(rows=[{'E': 'A123BC86', 'P': 'sts.pdf', 'L': '40'}]), {'sts.pdf': pdf('new')}), True)
    d = choose(c, d, cp_target=parents['cp'], documents={'v:5:sts': {'kind_id': kind, 'accept': True}})
    apply(c, d)
    assert current(c, parents['veh']) == before


def test_t21_three_valid_and_one_unselected_error_are_one_atomic_set(b5):
    c, db, _, parents, *_ = b5
    d = upload(c, workbook(rows=[{'I': f'A12{i}BC86', 'J': f'Valid {i}'} for i in (4, 5, 6)] + [{'I': 'invalid'}]))
    d = choose(c, d, cp_target=parents['cp'], vehicles={f'v:{r}': {'include': True, 'fields': ['name']} for r in (5, 6, 7)})
    saved, _ = apply(c, d)
    assert count(db, Vehicle) == 4 and len(saved['event_ids']) == 3
    assert {db.get(HistoryEvent, e).correlation_id for e in saved['event_ids']} == {saved['command_id']}


def test_t22_rejected_proposal_does_not_change_object(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'Rejected'})), cp_target=parents['cp'], cp_fields=[])
    before = snapshot(db)
    saved, _ = apply(c, d)
    assert saved['no_op'] and snapshot(db) == before


def test_t23_cancel_deletes_private_payload_and_temp_files_not_domain_objects(b5):
    c, db, _, parents, *_ = b5
    d = upload(c, transport(workbook({'B18': 'Cancelled'}), {'unbound.pdf': pdf()}), True)
    before = snapshot(db)
    stage_record = db.scalar(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == d['id']))
    from app.arm2.document_file_storage import private_path
    assert private_path(stage_record.id).exists()
    check(c.delete('/api/arm2/questionnaires/' + d['id']))
    assert snapshot(db) == before and not private_path(stage_record.id).exists()
    db.expire_all()
    assert db.get(QuestionnaireDraft, d['id']).payload == {} and reopen(c, d)['status'] == 'CANCELLED'


@pytest.mark.parametrize('failure', ['HIST', 'SQL', 'provider'])
def test_t24_selected_failure_rolls_back_whole_set_and_retries_private_upload(b5, failure):
    c, db, _, parents, _, disk = b5
    kind = doc_kind(c)
    d = upload(c, transport(workbook({'B18': 'Must rollback'}, [{'E': 'A123BC86', 'P': 'safe.pdf'}]), {'safe.pdf': pdf()}), True)
    d = choose(c, d, cp_target=parents['cp'], cp_fields=['phone'], documents={'v:5:sts': {'kind_id': kind, 'accept': True}})
    before = snapshot(db)
    def fail(connection, cursor, statement, parameters, context, many):
        if (failure == 'HIST' and 'INSERT INTO arm2_history_events' in statement) or (failure == 'SQL' and 'INSERT INTO arm2_questionnaire_receipts' in statement):
            raise SQLAlchemyError('Injected selected SQL failure')
    if failure == 'provider':
        disk.fail_at = 0
    else:
        event.listen(db.get_bind(), 'before_cursor_execute', fail)
    try:
        _, body = apply(c, d, status=503 if failure == 'provider' else 409)
        assert snapshot(db) == before and count(db, QuestionnaireReceipt) == 0
    finally:
        if failure != 'provider':
            event.remove(db.get_bind(), 'before_cursor_execute', fail)
        disk.fail_at = None
    saved, _ = apply(c, d, body=body)
    assert saved['event_ids'] and count(db, DocumentFile) == 1 and len(disk.uploads) == 1


def test_t24_stale_version_and_revocation_do_not_apply_any_selected_change(b5):
    c, db, user, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'stale'})), cp_target=parents['cp'], cp_fields=['phone'])
    original = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    check(c.put('/api/arm2/counterparties/' + parents['cp'], json={'name': original['name'], 'expected_version': original['version'], 'phone': 'Other operator'}))
    before = snapshot(db)
    apply(c, d, status=409)
    assert snapshot(db) == before
    d = reopen(c, d)
    assert d['approval_reset'] and d['choices']['cp_fields'] == []
    d = choose(c, d, cp_fields=['phone'])
    with Session(db.get_bind()) as other:
        actor = other.get(User, user.id)
        actor.role, actor.permissions = 'OPERATOR', ['arm2_cp_view']
        other.commit()
    apply(c, d, status=403)
    assert snapshot(db) == before


def test_t25_server_draft_reopens_in_new_authenticated_session(b5):
    c, db, user, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'Durable'})), cp_target=parents['cp'], cp_fields=['phone'])
    db.get_bind().dispose()
    with TestClient(c.app) as other:
        check(other.post('/api/auth/login', json={'login': user.login, 'password': 'document-test-password'}))
        other.headers['X-CSRF-Token'] = other.cookies['csrf']
        reopened = reopen(other, d)
        assert reopened['choices'] == d['choices'] and reopened['preview_id'] == d['preview_id']
        apply(other, reopened)
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['phone'] == 'Durable'


def test_t26_export_contains_all_vehicle_ids_without_other_cp_requisites(b5):
    c, db, _, parents, *_ = b5
    check(c.post('/api/arm2/counterparties', json={'name': 'DO_NOT_LEAK_OTHER_CP', 'inn': '1111111111'}), 201)
    for i in range(101, 251):
        db.add(Vehicle(id=f'VEH-{i:06}', name=f'Catalog {i}', plate_number=f'В{i}ВС86',
            accounting_state='Архивная' if i % 2 else 'Действующая', version=1))
    db.commit()
    data = exported(c, parents['cp'], 'xlsx')
    book = load_workbook(io.BytesIO(data))
    catalog = book['_ARM2_VEH']
    assert catalog.max_row == 152 and catalog.sheet_state == 'veryHidden'
    all_cells = ' '.join(str(cell.value) for ws in book for row in ws for cell in row)
    assert 'VEH-000250' in all_cells and 'DO_NOT_LEAK_OTHER_CP' not in all_cells and '1111111111' not in all_cells
    assert MANIFEST in book.sheetnames
    book.close()


def test_t27_missing_current_file_requires_explicit_incomplete_export_reason(b5):
    c, db, user, parents, _, disk = b5
    seed_document(b5)
    file = db.scalar(select(DocumentFile))
    disk.location(file.storage_ref).unlink()
    result = exported(c, parents['cp'])
    assert result['confirmation_required'] and result['missing'][0]['file_id'] == file.id
    before = snapshot(db)
    data = exported(c, parents['cp'], reason='Synthetic missing provider file acknowledged')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ['questionnaire.xlsx']
    audit = db.scalar(select(QuestionnaireExport))
    assert audit.author_id == user.id and audit.incomplete_reason and audit.missing
    assert snapshot(db) == before


def test_t28_three_dates_old_questionnaire_is_not_rejected(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B4': '21 октября 2026г', 'B18': 'Old but useful'})), cp_target=parents['cp'], cp_fields=['phone'])
    saved, _ = apply(c, d)
    assert saved['filled_date'] == '2026-10-21' and saved['uploaded_at'] < saved['accepted_at']
    assert 'payload' not in saved and 'phone' not in saved
    assert reopen(c, d)['receipt'] == saved


def test_t29_historic_review_type_link_is_preserved_and_not_newly_assigned(b5):
    c, db, _, parents, *_ = b5
    db.execute(text("INSERT INTO arm2_vehicle_types(id,name,status,version) VALUES('TYPE-700001','Legacy REVIEW','REVIEW',3)"))
    db.execute(text("UPDATE arm2_vehicles SET type_id='TYPE-700001' WHERE id=:id"), {'id': parents['veh']})
    db.commit()
    d = choose(c, upload(c, workbook(rows=[{'E': 'A123BC86', 'D': 'TYPE-700001', 'G': 'Changed model'}])),
        cp_target=parents['cp'], vehicles={'v:5': {'include': True, 'fields': ['model']}})
    apply(c, d)
    assert current(c, parents['veh'])['type_id'] == 'TYPE-700001'
    d = choose(c, upload(c, workbook(rows=[{'I': 'A124BC86', 'H': 'TYPE-700001'}])), cp_target=parents['cp'],
        vehicles={'v:5': {'include': True, 'fields': ['type_id']}})
    apply(c, d, status=422)


def test_t30_other_author_has_no_draft_file_or_receipt_access(b5):
    c, db, _, parents, *_ = b5
    d = upload(c, transport(workbook(), {'private.pdf': pdf()}), True)
    attachment = next(p['upload_id'] for p in d['comparison']['documents'] if p.get('upload_id'))
    other = User(login='b5-other', name='Synthetic other', role='ADMIN_OPERATOR', active=True, permissions=[],
                 password_hash=hasher.hash('synthetic-other-password'))
    db.add(other)
    db.commit()
    with TestClient(c.app) as client:
        check(client.post('/api/auth/login', json={'login': other.login, 'password': 'synthetic-other-password'}))
        client.headers['X-CSRF-Token'] = client.cookies['csrf']
        assert client.get('/api/arm2/questionnaires/' + d['id']).status_code == 403
        assert client.get(f"/api/arm2/questionnaires/{d['id']}/files/{attachment}/preview").status_code == 403
        assert client.delete('/api/arm2/questionnaires/' + d['id']).status_code == 403
        assert check(client.get('/api/arm2/questionnaires/receipts')) == []


def test_t31_zip_transport_is_never_a_document_or_file(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, transport(workbook({'B18': 'Only phone'}), {}), True), cp_target=parents['cp'], cp_fields=['phone'])
    apply(c, d)
    assert count(db, DocumentFile) == count(db, Document) == 0
    db.expire_all()
    assert db.get(QuestionnaireDraft, d['id']).payload == {}


def test_t32_nested_zip_is_unsupported_attachment_not_extracted(b5):
    c, db, _, parents, *_ = b5
    nested = transport(workbook(), {'hidden.pdf': pdf('must not extract')})
    d = upload(c, transport(workbook({'B18': 'Still useful'}, [{'E': 'A123BC86', 'P': 'sts.zip'}]), {'sts.zip': nested}), True)
    assert d['comparison']['documents'][0]['error']
    assert count(db, QuestionnaireUpload) == 1
    d = choose(c, d, cp_target=parents['cp'], cp_fields=['phone'])
    apply(c, d)
    assert count(db, DocumentFile) == 0


@pytest.mark.parametrize('format,suffix', [('PDF', 'pdf'), ('JPEG', 'jpg'), ('JPEG', 'jpeg'), ('PNG', 'png')])
def test_t33_verified_pdf_jpg_jpeg_png_preview_and_accept(b5, format, suffix):
    c, db, _, parents, *_ = b5
    data = pdf('verified')
    if format != 'PDF':
        output = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(output, format=format)
        data = output.getvalue()
    kind = doc_kind(c)
    filename = 'declared.' + suffix
    d = upload(c, transport(workbook(rows=[{'E': 'A123BC86', 'P': filename}]), {filename: data}), True)
    candidate = d['comparison']['documents'][0]
    response = c.get(f"/api/arm2/questionnaires/{d['id']}/files/{candidate['upload_id']}/preview")
    assert response.status_code == 200 and response.content == data and response.headers['x-content-type-options'] == 'nosniff'
    d = choose(c, d, cp_target=parents['cp'], documents={candidate['key']: {'kind_id': kind, 'accept': True}})
    apply(c, d)
    file = db.scalar(select(DocumentFile))
    assert file.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize('attack', ['slip', 'absolute', 'duplicate', 'symlink', 'external', 'xxe', 'macro'])
def test_transport_security_attacks_are_rejected_before_drafts(b5, attack):
    c, db, *_ = b5
    content = workbook()
    if attack in ('external', 'xxe', 'macro'):
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        if attack == 'macro':
            entries['xl/vbaProject.bin'] = b'forbidden'
        elif attack == 'xxe':
            entries['xl/workbook.xml'] = b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///private">]>' + entries['xl/workbook.xml']
        else:
            entries['xl/externalLinks/externalLink1.xml'] = b'<external/>'
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            for n, data in entries.items():
                z.writestr(n, data)
        content, filename = output.getvalue(), 'attack.xlsx'
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            z.writestr('questionnaire.xlsx', content)
            if attack == 'symlink':
                item = zipfile.ZipInfo('link.pdf')
                item.create_system, item.external_attr = 3, (stat.S_IFLNK | 0o777) << 16
                z.writestr(item, '/private')
            else:
                name = '../escape.pdf' if attack == 'slip' else '/absolute.pdf' if attack == 'absolute' else 'same.pdf'
                z.writestr(name, pdf())
                if attack == 'duplicate':
                    z.writestr(name, pdf())
        content, filename = output.getvalue(), 'attack.zip'
    response = c.post('/api/arm2/questionnaires', params={'filename': filename}, content=content)
    assert response.status_code == 422 and count(db, QuestionnaireDraft) == 0


def test_all_cp_rep_vehicle_document_classes_publish_together_with_final_versions(b5):
    c, db, *_ = b5
    kind = doc_kind(c)
    d = upload(c, transport(workbook({'B12': 'Brand new synthetic CP', 'B8': 'Normalized surname', 'B20': 'Separate signer'},
        [{'I': 'A888AA86', 'J': 'New fleet item', 'P': 'sts.pdf'}]), {'sts.pdf': pdf('all classes')}), True)
    d = choose(c, d, cp_target='new', cp_fields=['name', 'last_name'],
        representatives={'0': {'target': 'new', 'fields': ['name'], 'signer': True}},
        vehicles={'v:5': {'include': True, 'fields': ['name', 'owner']}},
        documents={'v:5:sts': {'kind_id': kind, 'accept': True}})
    saved, _ = apply(c, d)
    assert {o['kind'] for o in saved['objects']} == {'CP', 'CPREP', 'VEH', 'DOCUMENT'}
    cp = check(c.get('/api/arm2/counterparties/' + saved['cp_id']))
    assert cp['primary_signer_id'] and cp['last_name'] == 'Normalized surname'
    cp_receipt = next(o for o in saved['objects'] if o['kind'] == 'CP')
    assert cp_receipt['version'] == cp['version'] == 2
    vehicle = db.scalar(select(Vehicle).where(Vehicle.plate_number == 'А888АА86'))
    assert vehicle.owner_cp_id == cp['id']
    document = db.scalar(select(Document))
    assert document.parent_id == vehicle.id and count(db, DocumentFile) == 1
    assert {db.get(HistoryEvent, identifier).correlation_id for identifier in saved['event_ids']} == {saved['command_id']}
    assert reopen(c, d)['receipt'] == saved


def test_two_operators_same_version_only_one_atomic_command_can_win(b5):
    c, db, _, parents, *_ = b5
    actor = User(login='competing-operator', name='Synthetic competing operator', role='ADMIN_OPERATOR', active=True,
                 permissions=[], password_hash=hasher.hash('synthetic-competing-password'))
    db.add(actor)
    db.commit()
    first = choose(c, upload(c, workbook({'B18': 'First operator'})), cp_target=parents['cp'], cp_fields=['phone'])
    with TestClient(c.app) as other:
        check(other.post('/api/auth/login', json={'login': actor.login, 'password': 'synthetic-competing-password'}))
        other.headers['X-CSRF-Token'] = other.cookies['csrf']
        second = choose(other, upload(other, workbook({'B18': 'Second operator'})), cp_target=parents['cp'], cp_fields=['phone'])
        def submit(client, draft):
            return client.post('/api/arm2/questionnaires/' + draft['id'] + '/apply', json={
                'command_id': str(uuid4()), 'revision': draft['revision'], 'preview_id': draft['preview_id']})
        with ThreadPoolExecutor(2) as pool:
            tasks = [pool.submit(submit, c, first), pool.submit(submit, other, second)]
            responses = [task.result(30) for task in tasks]
        assert sorted(r.status_code for r in responses) == [200, 409]
        loser_client, loser = (c, first) if responses[0].status_code == 409 else (other, second)
        refreshed = reopen(loser_client, loser)
        assert refreshed['approval_reset'] and refreshed['choices']['cp_fields'] == []
    assert count(db, QuestionnaireReceipt) == 1
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['version'] == 2


def test_unselected_changed_vehicle_does_not_veto_selected_cp_change(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'Independent CP edit'}, [{'E': 'A123BC86'}])),
               cp_target=parents['cp'], cp_fields=['phone'])
    v = current(c, parents['veh'])
    check(command(c, v, ('CP', parents['cp'])))
    saved, _ = apply(c, d)
    assert len(saved['event_ids']) == 1
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['phone'] == 'Independent CP edit'


def test_selected_missing_file_rejects_whole_set_and_representative_user_injection_is_forbidden(b5):
    c, db, _, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'Must not commit'}, [{'E': 'A123BC86', 'P': 'missing.pdf'}])),
               cp_target=parents['cp'], cp_fields=['phone'], documents={'v:5:sts': {'accept': True}})
    before = snapshot(db)
    apply(c, d, status=422)
    assert snapshot(db) == before
    response = c.put('/api/arm2/questionnaires/' + d['id'], json={'revision': d['revision'], 'choices': {
        'cp_target': parents['cp'], 'representatives': {'0': {'target': 'new', 'fields': ['user_id'], 'user_id': 1}}}})
    assert response.status_code == 422


def test_cp_viewer_cannot_write_other_cp_or_its_documents(b5):
    c, db, user, parents, *_ = b5
    d = choose(c, upload(c, workbook({'B18': 'Denied'})), cp_target=parents['cp'], cp_fields=['phone'])
    with Session(db.get_bind()) as administrator:
        actor = administrator.get(User, user.id)
        actor.role, actor.permissions = 'OPERATOR', ['arm2_cp_view', 'arm2_document_view']
        administrator.commit()
    before = snapshot(db)
    apply(c, d, status=403)
    assert snapshot(db) == before
