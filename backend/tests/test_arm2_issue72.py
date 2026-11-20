"""Six focused acceptance paths on disposable DB/FILE; never a live UAT reset."""
import io
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import event, select, text
from test_arm2_document_file_api import accept, stage
from test_arm2_questionnaire_workflow import api as api
from test_arm2_questionnaire_workflow import (
    apply,
    check,
    choose,
    doc_kind,
    exported,
    snapshot,
    transport,
    upload,
    workbook,
)
from test_arm2_questionnaire_workflow import b5 as b5
from test_arm2_questionnaire_workflow import qa as qa

from app.arm2.counterparty_rules import LEGAL, PERSON
from app.arm2.uat_reset import MARKER, TABLES, reset
from app.arm2.vehicle_models import Vehicle
from app.config import settings
from app.db import engine
from app.models import User


def cp(c, name='Synthetic', **values):
    return check(c.post('/api/arm2/counterparties', json={'type': LEGAL, 'name': name, **values}), 201)


def v4(values=None):
    return workbook({'B3': 'ARM_IMPORT_V4', 'B5': LEGAL, 'B7': 'АО', 'B12': 'Synthetic V4', **(values or {})})


def test_i01_creation_type_names_and_existing_type_gate(b5):
    c, db, _, parents, *_ = b5
    for typ, opf in [(PERSON, None), (PERSON, 'ИП'), (LEGAL, 'ООО'), (LEGAL, 'АО'), (LEGAL, 'МУП')]:
        row = cp(c, type=typ, legal_form=opf, last_name='Тестов' if typ == PERSON else None,
                 first_name='Иван' if typ == PERSON else None)
        opened = check(c.get('/api/arm2/counterparties/' + row['id']))
        assert opened['type'] == typ and opened['legal_form'] == opf and 'status' not in opened
        if typ == PERSON:
            assert opened['full_name'] == ('Индивидуальный предприниматель ' if opf else '') + 'Тестов Иван'
            assert db.scalar(text('SELECT full_name FROM arm2_counterparties WHERE id=:id'), {'id': row['id']}) is None
            db.rollback()
    for body in ({'name': 'No type'}, {'type': PERSON}, {'type': PERSON, 'name': ' '},
                 {'type': PERSON, 'name': 'Invalid OPF', 'legal_form': 'ООО'}):
        assert c.post('/api/arm2/counterparties', json=body).status_code == 422
    assert 'status' not in check(c.get('/api/arm2/organizations/' + parents['org']))
    typ = check(c.post('/api/arm2/types', json={'name': 'Incomplete TYPE'}), 201)
    assert typ['status'] == 'REVIEW'
    assert c.post('/api/arm2/vehicles', json={'name': 'Must reject', 'plate_number': 'A222BC86', 'type_id': typ['id']}).status_code == 422


def test_i02_manual_transition_explicit_clears_history_and_409(b5):
    c, db, *_ = b5
    original = cp(c, full_name='Old legal full', short_name='Old legal short', legal_form='ООО', kpp='001234567',
                  ogrn='0012345678901', bik='001234567', settlement_account='00000000000000000001')
    rep = check(c.post(f"/api/arm2/counterparties/{original['id']}/representatives", json={'name': 'Signer'}), 201)
    original = check(c.get('/api/arm2/counterparties/' + original['id']))
    before = snapshot(db)
    body = {'expected_version': original['version'], 'name': original['name'], 'type': PERSON, 'legal_form': 'ИП',
            'last_name': 'Тестов', 'first_name': 'Иван', 'full_name': None, 'short_name': None, 'kpp': None, 'ogrn': None}
    path = '/api/arm2/counterparties/' + original['id']
    assert c.put(path, json=body).status_code == 422
    assert snapshot(db) == before
    assert c.put(path, json={**body, 'confirm_identity_change': True, 'expected_version': original['version'] + 99}).status_code == 409
    assert snapshot(db) == before
    changed = check(c.put(path, json={**body, 'confirm_identity_change': True}))
    assert changed['id'] == original['id'] and changed['name'] == original['name']
    assert changed['full_name'] == 'Индивидуальный предприниматель Тестов Иван'
    assert changed['kpp'] is None and changed['ogrn'] is None and changed['bik'] == '001234567'
    assert changed['settlement_account'] == '00000000000000000001'
    assert check(c.get(path + '/representatives'))[0]['id'] == rep['id']
    history = check(c.get('/api/arm2/history/CP/' + original['id']))
    detail = check(c.get('/api/arm2/history/events/' + str(history[0]['event_id'])))
    assert detail['changes']['full_name'] == {'before': 'Old legal full', 'after': None}
    changed = check(c.put(path, json={'expected_version': changed['version'], 'name': changed['name'],
                                    'legal_form': None, 'confirm_identity_change': True}))
    assert changed['full_name'] == 'Тестов Иван'


def test_i03_v4_and_legacy_roundtrip_keep_clear_and_documents(b5):
    c, db, _, parents, *_ = b5
    record = cp(c, name='Keep working name', full_name='Legal full', short_name='Legal short', legal_form='АО')
    kind = doc_kind(c, 'Synthetic CP document', 'CP')
    base = f"/api/arm2/cp/{record['id']}/documents"
    document = check(c.post(base, json={'kind_ref': kind, 'name': 'Synthetic original'}), 201)
    url = base + '/' + document['id']
    content = b'%PDF-1.4\nSynthetic only\n%%EOF\n'
    assert accept(c, url, 1, [stage(c, url, content)]).status_code == 200
    exported_book = exported(c, record['id'], 'xlsx')
    book = load_workbook(io.BytesIO(exported_book))
    passport = book['Паспорт контрагента']
    assert [passport[f'B{r}'].value for r in (3, 5, 6, 7)] == ['ARM_IMPORT_V4', LEGAL, record['id'], 'АО']
    assert 'Представители' in book.sheetnames and 'Техника' in book.sheetnames
    assert any('B5' in str(v.sqref) for v in passport.data_validations.dataValidation)
    book.close()
    for data, zipped in [(exported_book, False), (exported(c, record['id'], 'zip'), True)]:
        d = upload(c, data, zip=zipped)
        d = choose(c, d, cp_target=record['id'])
        saved, _ = apply(c, d)
        assert saved['no_op'] and not saved['event_ids']
    for legacy, typ, opf in [('ИП', PERSON, 'ИП'), ('Физическое лицо', PERSON, ''), ('ООО', LEGAL, 'ООО'), ('Иное юридическое лицо', LEGAL, '')]:
        from app.arm2.questionnaire_parse import parse
        parsed, _ = parse(workbook({'B5': legacy, 'B7': 'MUST IGNORE'}), 'legacy.xlsx')
        assert parsed['cp']['type'] == typ and parsed['cp']['legal_form'] == opf
    before = check(c.get('/api/arm2/counterparties/' + record['id']))
    d = upload(c, v4({'B5': PERSON, 'B7': 'ИП', 'B8': 'Тестов', 'B9': 'Иван', 'B12': 'Not the working name'}))
    d = choose(c, d, cp_target=record['id'], cp_fields=['type', 'legal_form', 'last_name', 'first_name'])
    assert d['comparison']['cp']['fields']['full_name']['clear_candidate']
    _, _ = apply(c, d, 409)
    d = choose(c, d, cp_clears=['full_name', 'short_name'], confirm_identity=True)
    saved, body = apply(c, d)
    assert saved['changes'] and check(c.post('/api/arm2/questionnaires/' + d['id'] + '/apply', json=body)) == saved
    after = check(c.get('/api/arm2/counterparties/' + record['id']))
    assert after['name'] == before['name'] and after['type'] == PERSON
    assert len(check(c.get(base))) == 1
    empty = upload(c, v4({'B5': PERSON, 'B7': '', 'B18': 'New phone'}))
    empty = choose(c, empty, cp_target=record['id'], cp_fields=['phone'])
    apply(c, empty)
    assert check(c.get('/api/arm2/counterparties/' + record['id']))['legal_form'] == 'ИП'


def test_i04_dedup_cancel_changed_content_and_reopen(b5):
    c, db, *_ = b5
    before = snapshot(db)
    data = v4()
    first = upload(c, data)
    assert upload(c, data)['id'] == first['id']
    first = choose(c, first, cp_target='new', cp_fields=['type', 'name'])
    assert upload(c, data)['choices'] == first['choices']
    check(c.delete('/api/arm2/questionnaires/' + first['id']))
    assert snapshot(db) == before
    second = upload(c, data)
    assert second['id'] != first['id']
    assert upload(c, v4({'B18': 'Changed file'}))['id'] != second['id']


def test_unselected_invalid_identity_proposal_does_not_poison_a_phone_update(b5):
    c, _, _, parents, *_ = b5
    original = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    d = upload(c, v4({'B5': PERSON, 'B7': 'ООО', 'B18': 'Independent phone'}))
    d = choose(c, d, cp_target=parents['cp'], cp_fields=['phone'])
    assert d['comparison']['cp']['fields']['legal_form']['error']
    apply(c, d)
    current = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    assert current['phone'] == 'Independent phone' and current['type'] == original['type']
    assert current['legal_form'] == original['legal_form']


def test_i05_full_query_counts_pagination_empty_and_permissions(b5):
    c, db, user, parents, *_ = b5
    expected = []
    for i in range(105):
        row = Vehicle(id=f'VEH-{900000+i:06}', version=1, name='Count fixture', plate_number=f'А{i:03}ВС86',
            accounting_state='Действующая' if i < 80 else 'Архивная', owner_cp_id=parents['cp'] if i % 2 else None,
            wialon_unit_id=None if i % 3 else '  ' if i % 6 else 'test-unit')
        db.add(row)
        expected.append(row)
    db.commit()
    for state in ('', 'Действующая', 'Архивная'):
        rows = [r for r in expected if not state or r.accounting_state == state]
        golden = (len(rows), sum(r.owner_cp_id is None for r in rows), sum(not (r.wialon_unit_id or '').strip() for r in rows))
        for offset in (0, 50, 100):
            page = check(c.get('/api/arm2/vehicles/search', params={'q': 'Count fixture', 'limit': 1, 'offset': offset,
                **({'state': state} if state else {})}))
            assert (page['matched'], page['without_owner'], page['without_wialon']) == golden
    page = check(c.get('/api/arm2/vehicles/search?q=NO_MATCH'))
    assert page['matched'] == page['without_owner'] == page['without_wialon'] == 0
    assert c.get('/api/arm2/vehicles/search?state=ALL').status_code == 422
    user.role, user.permissions = 'OPERATOR', ['arm2_veh_view']
    db.commit()
    assert c.get('/api/arm2/vehicles/search?owner_kind=CP').status_code == 403
    assert check(c.get('/api/arm2/vehicles/search?q=Count%20fixture'))['without_owner'] == sum(r.owner_cp_id is None for r in expected)


def test_i06_offline_reset_identity_recovery_bootstrap_and_new_v4(b5, monkeypatch):
    c, db, _, _, _, fake = b5
    root = Path(settings().data_dir)
    root.mkdir(exist_ok=True, parents=True)
    (root / 'arm2-document-runtime-v1').mkdir(exist_ok=True)
    (root / 'arm2-document-runtime-v1' / ('a' * 64)).write_bytes(b'Synthetic stage')
    (fake.path / ('b' * 64)).write_bytes(b'Synthetic FILE')
    upload(c, transport(v4(), {'synthetic.pdf': b'%PDF-1.4\nSynthetic\n%%EOF'}), zip=True)
    before = snapshot(db)
    users = db.execute(select(User.id, User.login, User.password_hash, User.role, User.permissions)).all()
    db.rollback()
    attest = {'project': 'arm2-ubuntu-uat-r2', 'file_volume': 'arm2-ubuntu-uat-r2_r2_file_data',
              'data_volume': 'arm2-ubuntu-uat-r2_r2_photo_data', 'offline': True}
    with pytest.raises(ValueError, match='B1|R2'):
        reset(engine(), {**attest, 'project': 'arm2-ubuntu-uat'}, fake.path, root, execute=True)
    assert snapshot(db) == before
    db.rollback()
    monkeypatch.setenv('APP_ENV', 'development')
    settings.cache_clear()
    with pytest.raises(ValueError, match='test-only'):
        reset(engine(), attest, fake.path, root, execute=True)
    monkeypatch.setenv('APP_ENV', 'test')
    settings.cache_clear()
    failed = False

    def fail_sql(connection, cursor, statement, parameters, context, executemany):
        nonlocal failed
        if statement.startswith('TRUNCATE') and not failed:
            failed = True
            raise RuntimeError('Synthetic reset interruption')

    event.listen(engine(), 'before_cursor_execute', fail_sql)
    with pytest.raises(RuntimeError, match='interruption'):
        reset(engine(), attest, fake.path, root, execute=True)
    event.remove(engine(), 'before_cursor_execute', fail_sql)
    assert snapshot(db) == before
    db.rollback()
    for _ in range(2):
        result = reset(engine(), attest, fake.path, root, execute=True)
        assert not any(result['counts'].values()) and result['bootstrap_preserved']
        assert {p.name for p in fake.path.iterdir()} == {MARKER}
    assert db.execute(select(User.id, User.login, User.password_hash, User.role, User.permissions)).all() == users
    db.rollback()
    assert c.post('/api/auth/login', json={'login': 'document-admin', 'password': 'document-test-password'}).status_code == 200
    c.headers['X-CSRF-Token'] = c.cookies['csrf']
    d = choose(c, upload(c, v4()), cp_target='new', cp_fields=['name', 'type', 'legal_form'])
    saved, _ = apply(c, d)
    assert saved['cp_id'] and not saved['no_op']
    assert check(c.get('/api/arm2/counterparties/' + saved['cp_id']))['legal_form'] == 'АО'
    assert len(TABLES) == 23 and json.loads((fake.path / MARKER).read_text())['project'] == attest['project']
