"""Exchange regression: only synthetic values in the extracted legacy V2 structure."""
import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from test_arm2_questionnaire_workflow import api as api
from test_arm2_questionnaire_workflow import (
    apply,
    check,
    choose,
    doc_kind,
    exported,
    pdf,
    snapshot,
    transport,
    upload,
    workbook,
)
from test_arm2_questionnaire_workflow import (
    b5 as b5,
)
from test_arm2_questionnaire_workflow import qa as qa
from test_arm2_vehicle_links import command, current

from app.arm2.questionnaire_parse import EXPORT_VERSION, PASSPORT, REPRESENTATIVES, VEHICLES, parse
from app.arm2.representative_models import CounterpartyRepresentative
from app.auth import hasher
from app.models import User


def legacy_fixture(text_accounts=False, formula_date=False):
    fixture = json.loads((Path(__file__).parent / 'fixtures/arm_import_v2_anonymized.json').read_text(encoding='utf-8'))
    if text_accounts:
        for coordinate, value in [('B24', '001234567'), ('B25', '00000000000000000001'), ('B26', '00000000000000000002')]:
            fixture['sheets']['Паспорт контрагента'][coordinate] = {'value': value, 'format': '@'}
    if formula_date:
        fixture['sheets']['Паспорт контрагента']['B4'] = {'value': '=DATE(2026,10,21)', 'format': '@'}
        fixture['caches']['Паспорт контрагента'] = {'B4': '2026-10-21'}
    book = Workbook()
    book.remove(book.active)
    for title, cells in fixture['sheets'].items():
        sheet = book.create_sheet(title)
        for coordinate, spec in cells.items():
            sheet[coordinate] = spec['value']
            sheet[coordinate].number_format = spec['format']
        for coordinate, target in fixture.get('hyperlinks', {}).get(title, {}).items():
            sheet[coordinate].hyperlink = target
        if title == 'Справочники':
            sheet.sheet_state = 'hidden'
    out = io.BytesIO()
    book.save(out)
    cached = io.BytesIO()
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as source, zipfile.ZipFile(cached, 'w', zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item)
            cached_cells = next((fixture['caches'].get(title, {}) for n, title in enumerate(fixture['sheets'], 1)
                                 if item.filename == f'xl/worksheets/sheet{n}.xml'), {})
            if cached_cells:
                root = ET.fromstring(data)
                for cell in root.iter(ns + 'c'):
                    if cell.get('r') in cached_cells:
                        cell.set('t', 'str')
                        value = cell.find(ns + 'v')
                        if value is None:
                            value = ET.SubElement(cell, ns + 'v')
                        value.text = cached_cells[cell.get('r')]
                data = ET.tostring(root, encoding='utf-8')
            target.writestr(item.filename, data)
    return cached.getvalue()


def test_supplied_legacy_v2_structure_accepts_formula_caches_without_new_ids_or_reps():
    parsed, blobs = parse(legacy_fixture(), 'anonymized.xlsx')
    assert parsed['schema'] == 'ARM_IMPORT_V2' and not blobs
    assert len(parsed['vehicles']) == 12
    assert parsed['vehicles'][0]['plate_number'] == 'A001AA86'
    assert parsed['vehicles'][0]['legacy_hint'] == 'V3-001'
    assert all(not v['id'] for v in parsed['vehicles'])
    assert parsed['cp']['last_name'] == 'Synthetic' and parsed['cp']['inn'] == '1234567890'
    assert parsed['cp_hint'] == 'Synthetic legacy CP' and parsed['representatives'] == []
    assert 'IGNORED LEGACY VALUE' not in str(parsed)
    assert set(parsed['cp_errors']) == {'settlement_account', 'correspondent_account'}


def test_legacy_cached_date_and_text_bank_identifiers_keep_exact_leading_zeros():
    parsed, _ = parse(legacy_fixture(text_accounts=True, formula_date=True), 'anonymized.xlsx')
    assert parsed['filled_date'] == '2026-10-21'
    assert parsed['cp']['bik'] == '001234567'
    assert parsed['cp']['settlement_account'] == '00000000000000000001'
    assert parsed['cp']['correspondent_account'] == '00000000000000000002'
    assert parsed['cp_errors'] == {}


def test_live_cp_id_hint_adds_a_candidate_without_selecting_or_updating_it(b5):
    c, _, _, parents, *_ = b5
    draft = upload(c, workbook({'B6': parents['cp'], 'B12': 'Different proposed name'}))
    comparison = draft['comparison']['cp']
    assert parents['cp'] in {candidate['id'] for candidate in comparison['candidates']}
    assert comparison['target'] is None and comparison['id'] is None
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['name'] == 'CP'


def test_legacy_numeric_accounts_require_clarification_but_other_fields_and_document_bindings_work(b5):
    c, _, _, parents, *_ = b5
    existing = check(c.post('/api/arm2/vehicles', json={'name': 'Existing legacy vehicle',
        'plate_number': 'А001АА86'}), 201)
    attachments = {'sts-5.pdf': pdf('synthetic STS'), 'company.pdf': pdf('synthetic company')}
    draft = upload(c, transport(legacy_fixture(), attachments), True)
    kind = doc_kind(c)
    cp_kind = doc_kind(c, 'Карточка предприятия', 'CP')
    draft = choose(c, draft, cp_target=parents['cp'], cp_fields=['phone', 'settlement_account'])
    assert draft['comparison']['cp']['fields']['settlement_account']['error']
    apply(c, draft, 422)
    draft = choose(c, draft, cp_fields=['phone'], documents={
        'v:5:sts': {'kind_id': kind, 'accept': True}, 'company': {'kind_id': cp_kind, 'accept': True}})
    bound = {d['key']: d for d in draft['comparison']['documents']}
    assert bound['v:5:sts']['parent_key'] == 'v:5' and bound['company']['parent_key'] == 'CP'
    assert bound['v:5:sts']['parent_id'] == existing['id']
    assert draft['comparison']['vehicles'][0]['id'] == existing['id']
    receipt, _ = apply(c, draft)
    assert {o['kind'] for o in receipt['objects']} == {'CP', 'DOCUMENT'}
    assert len([o for o in receipt['objects'] if o['kind'] == 'DOCUMENT']) == 2
    assert current(c, existing['id'])['name'] == 'Existing legacy vehicle'
    cp = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    assert cp['phone'] == 'Synthetic phone' and cp['settlement_account'] is None


def seeded_exchange(b5):
    c, _, user, parents, *_ = b5
    root = '/api/arm2/counterparties/' + parents['cp']
    reps = []
    for fields in [{'name': 'Synthetic signer', 'position': 'Director', 'authority_basis': 'Synthetic authority',
                    'phone': 'Contact 1', 'email': 'signer@example.invalid', 'user_id': user.id},
                   {'name': 'Synthetic signer', 'position': 'Separate person', 'state': 'Архивный'},
                   {'name': 'Contact', 'phone': 'Contact 3'}]:
        reps.append(check(c.post(root + '/representatives', json=fields), 201))
    cp = check(c.get(root))
    check(c.put(root, json={'name': cp['name'], 'expected_version': cp['version'],
                            'primary_signer_id': reps[0]['id'], 'bik': '001234567',
                            'settlement_account': '00000000000000000001'}))
    check(command(c, current(c, parents['veh']), ('CP', parents['cp'])))
    return reps


def edit_export(data, edit):
    """Keep server formula caches for untouched formulae; never run Excel formulae."""
    book = load_workbook(io.BytesIO(data))
    cached = load_workbook(io.BytesIO(data), data_only=True)
    values = {s.title: {cell.coordinate: cached[s.title][cell.coordinate].value for row in s
                       for cell in row if cell.data_type == 'f'} for s in book}
    edit(book)
    files = {f'xl/worksheets/sheet{i}.xml': s.title for i, s in enumerate(book, 1)}
    out = io.BytesIO()
    book.save(out)
    book.close()
    cached.close()
    result = io.BytesIO()
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as source, zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            data = source.read(entry)
            cache = values.get(files.get(entry.filename), {})
            if cache:
                root = ET.fromstring(data)
                for cell in root.iter(ns + 'c'):
                    if cell.find(ns + 'f') is not None and cell.get('r') in cache:
                        cell.set('t', 'str')
                        value = cell.find(ns + 'v')
                        if value is None:
                            value = ET.SubElement(cell, ns + 'v')
                        value.text = str(cache[cell.get('r')] or '')
                data = ET.tostring(root, encoding='utf-8')
            target.writestr(entry.filename, data)
    return result.getvalue()


def test_v3_export_full_contacts_signer_projections_ids_access_formulas_and_validations(b5):
    c, db, user, parents, *_ = b5
    reps = seeded_exchange(b5)
    data = exported(c, parents['cp'], 'xlsx')
    book = load_workbook(io.BytesIO(data))
    cached = load_workbook(io.BytesIO(data), data_only=True)
    assert book[PASSPORT]['B3'].value == EXPORT_VERSION == 'ARM_IMPORT_V3'
    assert cached[PASSPORT]['B20'].value == cached[REPRESENTATIVES]['C5'].value == reps[0]['name']
    assert book[PASSPORT]['B20'].data_type == 'f'
    assert book[REPRESENTATIVES]['B5'].value == reps[0]['id']
    assert book[REPRESENTATIVES]['B6'].value == reps[1]['id']
    assert book[REPRESENTATIVES]['H6'].value == 'Архивный'
    assert book[REPRESENTATIVES]['J5'].value == f'{user.id} · {user.login}'
    assert book[REPRESENTATIVES]['I5'].value == 'Есть доступ к АРМ'
    assert book[REPRESENTATIVES]['I6'].value == 'Нет связанной учётной записи'
    assert cached[PASSPORT]['B24'].value == '001234567' and cached[PASSPORT]['B25'].value == '00000000000000000001'
    assert book[PASSPORT]['B25'].number_format == '@'
    assert book['Справочники'].sheet_state == 'veryHidden'
    assert cached[VEHICLES]['B5'].value == parents['veh']
    assert cached[VEHICLES]['C5'].value == 'ИЗ СПРАВОЧНИКА'
    assert cached[VEHICLES]['E5'].value == current(c, parents['veh'])['plate_number']
    assert cached[VEHICLES]['S5'].value == parents['cp']
    validations = {str(v.sqref): v.formula1 for v in book[VEHICLES].data_validations.dataValidation}
    assert validations['C5:C1004'] == '"ИЗ СПРАВОЧНИКА,НОВАЯ,ВЫБЫЛО"'
    assert validations['B5:B1004'] == 'ARM2_VEH_IDS' and validations['H5:H1004'] == 'ARM2_TYPE_IDS'
    assert book.defined_names['ARM2_VEH_IDS'].attr_text.startswith("'Справочники'!")
    parsed, _ = parse(data, 'v3.xlsx')
    assert len(parsed['representatives']) == 3 and len(parsed['vehicles']) == 1
    assert parsed['representative_meta'][0]['conflicts'] == {}
    assert all(set(rep) <= {'name', 'position', 'authority_basis', 'phone', 'email', 'state'} for rep in parsed['representatives'])
    before = snapshot(db)
    for _ in range(2):
        draft = choose(c, upload(c, data), cp_target=parents['cp'])
        assert all(f['equal'] for rep in draft['comparison']['representatives'] for f in rep['fields'].values())
        receipt, _ = apply(c, draft)
        assert receipt['no_op'] and receipt['event_ids'] == []
        assert snapshot(db) == before
    book.close()
    cached.close()


def test_v3_contact_archive_restore_and_new_contact_never_change_user_access(b5):
    c, db, user, parents, *_ = b5
    reps = seeded_exchange(b5)
    db.expire_all()
    user_before = (db.get(User, user.id).active, db.get(User, user.id).role, list(db.get(User, user.id).permissions))
    data = exported(c, parents['cp'], 'xlsx')

    def edit(book):
        sheet = book[REPRESENTATIVES]
        for coordinate, value in {'E5': 'New contact phone', 'H5': 'Архивный', 'I5': 'BLOCK USER',
                'J5': '999999', 'L5': 'Synthetic review note', 'H6': 'Действующий', 'C8': 'New person', 'D8': 'Operator',
                'E8': 'New phone', 'I8': 'ADMIN_OPERATOR', 'J8': str(user.id)}.items():
            sheet[coordinate] = value
    draft = choose(c, upload(c, edit_export(data, edit)), cp_target=parents['cp'], representatives={
        '0': {'target': reps[0]['id'], 'fields': ['phone', 'state']},
        '1': {'target': reps[1]['id'], 'fields': ['state']},
        '3': {'target': 'new', 'fields': ['name', 'position', 'phone']}})
    assert draft['comparison']['representatives'][0]['source_comment'] == 'Synthetic review note'
    assert 'comment' not in draft['comparison']['representatives'][0]['fields']
    receipt, _ = apply(c, draft)
    assert len([o for o in receipt['objects'] if o['kind'] == 'CPREP']) == 3
    db.expire_all()
    assert db.get(CounterpartyRepresentative, reps[0]['id']).state == 'Архивный'
    assert db.get(CounterpartyRepresentative, reps[0]['id']).phone == 'New contact phone'
    assert db.get(CounterpartyRepresentative, reps[0]['id']).user_id == user.id
    assert db.get(CounterpartyRepresentative, reps[1]['id']).state == 'Действующий'
    new = db.scalar(select(CounterpartyRepresentative).where(CounterpartyRepresentative.name == 'New person'))
    assert new.user_id is None
    assert (db.get(User, user.id).active, db.get(User, user.id).role, list(db.get(User, user.id).permissions)) == user_before


@pytest.mark.parametrize('source,expected', [('passport', 'Passport proposal'), ('list', 'List proposal')])
def test_v3_signer_conflicts_have_no_automatic_priority_and_update_one_record(b5, source, expected):
    c, db, _, parents, *_ = b5
    reps = seeded_exchange(b5)
    data = exported(c, parents['cp'], 'xlsx')

    def edit(book):
        book[PASSPORT]['B20'] = 'Passport proposal'
        book[REPRESENTATIVES]['C5'] = 'List proposal'
    draft = choose(c, upload(c, edit_export(data, edit)), cp_target=parents['cp'],
        representatives={'0': {'target': reps[0]['id'], 'fields': ['name'], 'signer': True}})
    assert len(draft['comparison']['representatives']) == 3
    rep = draft['comparison']['representatives'][0]
    assert rep['conflicts']['name'] == {'passport': 'Passport proposal', 'list': 'List proposal'}
    assert rep['fields']['name']['error']
    before = snapshot(db)
    apply(c, draft, 422)
    assert snapshot(db) == before
    draft = choose(c, draft, representatives={'0': {'target': reps[0]['id'], 'fields': ['name'],
                                                  'signer': True, 'sources': {'name': source}}})
    assert draft['choices']['representatives']['0']['fields'] == []
    draft = choose(c, draft, representatives={'0': {'target': reps[0]['id'], 'fields': ['name'],
                                                  'signer': True, 'sources': {'name': source}}})
    receipt, _ = apply(c, draft)
    assert len(receipt['event_ids']) == 1
    db.expire_all()
    assert db.get(CounterpartyRepresentative, reps[0]['id']).name == expected
    assert len(list(db.scalars(select(CounterpartyRepresentative)))) == 3
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['primary_signer_id'] == reps[0]['id']


def test_v3_missing_sheet_fields_and_rows_do_not_delete_contacts_or_signer(b5):
    c, db, _, parents, *_ = b5
    reps = seeded_exchange(b5)
    data = exported(c, parents['cp'], 'xlsx')

    def edit(book):
        book.remove(book[REPRESENTATIVES])
        for coord in ['B20', 'B21', 'B22', 'B24', 'B25']:
            book[PASSPORT][coord] = None
    before = snapshot(db)
    draft = choose(c, upload(c, edit_export(data, edit)), cp_target=parents['cp'])
    receipt, _ = apply(c, draft)
    assert receipt['no_op'] and snapshot(db) == before
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['primary_signer_id'] == reps[0]['id']


@pytest.mark.parametrize('version', ['ARM_IMPORT_V4', 'ПРОЕКТ_ARM2_B5', 'ARM_IMPORT_V30'])
def test_unknown_or_project_versions_are_not_silently_interpreted_as_v2(version):
    with pytest.raises(ValueError, match='ARM_IMPORT_V2.*ARM_IMPORT_V3'):
        parse(workbook({'B3': version}), 'unsupported.xlsx')


def test_unsigned_v3_without_ids_keeps_one_signer_proposal_and_requires_explicit_matching(b5):
    c, db, _, parents, *_ = b5

    def extra(book):
        sheet = book.create_sheet(REPRESENTATIVES)
        sheet['C5'], sheet['D5'], sheet['K5'] = 'Synthetic new signer', 'Director', 'ДА'
    data = workbook({'B3': EXPORT_VERSION, 'B20': 'Synthetic new signer', 'B21': 'Director'}, extra=extra)
    parsed, _ = parse(data, 'unsigned.xlsx')
    assert len(parsed['representatives']) == 1
    draft = choose(c, upload(c, data), cp_target=parents['cp'], representatives={
        '0': {'target': 'new', 'fields': ['name', 'position'], 'signer': True}})
    first, _ = apply(c, draft)
    rep_id = next(o['id'] for o in first['objects'] if o['kind'] == 'CPREP')
    before = snapshot(db)
    draft = choose(c, upload(c, data), cp_target=parents['cp'], representatives={
        '0': {'target': rep_id, 'fields': ['name', 'position'], 'signer': True}})
    assert len(draft['comparison']['representatives']) == 1
    second, _ = apply(c, draft)
    assert second['no_op'] and snapshot(db) == before
    assert len(list(db.scalars(select(CounterpartyRepresentative)))) == 1


@pytest.mark.parametrize('bad_identity', ['duplicate', 'foreign', 'new-for-existing'])
def test_v3_representative_identity_cannot_duplicate_or_cross_cp_boundary(b5, bad_identity):
    c, db, _, parents, *_ = b5
    reps = seeded_exchange(b5)
    foreign_cp = check(c.post('/api/arm2/counterparties', json={'name': 'Synthetic foreign CP'}), 201)
    foreign_rep = check(c.post('/api/arm2/counterparties/' + foreign_cp['id'] + '/representatives',
                              json={'name': 'Synthetic foreign representative'}), 201)
    data = exported(c, parents['cp'], 'xlsx')

    def edit(book):
        if bad_identity == 'duplicate':
            book[REPRESENTATIVES]['B6'] = reps[0]['id']
        elif bad_identity == 'foreign':
            book[REPRESENTATIVES]['B5'] = foreign_rep['id']
        book[REPRESENTATIVES]['E5'] = 'Unaccepted change'
    draft = choose(c, upload(c, edit_export(data, edit)), cp_target=parents['cp'], representatives={
        '0': {'target': 'new' if bad_identity == 'new-for-existing' else reps[0]['id'], 'fields': ['phone']}})
    assert draft['comparison']['representatives'][0]['error']
    before = snapshot(db)
    apply(c, draft, 422)
    assert snapshot(db) == before


def test_v3_signer_reassignment_changes_only_the_cp_link_and_reexports_one_projection(b5):
    c, db, _, parents, *_ = b5
    reps = seeded_exchange(b5)
    data = exported(c, parents['cp'], 'xlsx')
    draft = choose(c, upload(c, data), cp_target=parents['cp'],
        representatives={'2': {'target': reps[2]['id'], 'fields': [], 'signer': True}})
    receipt, _ = apply(c, draft)
    assert len(receipt['event_ids']) == 1
    assert check(c.get('/api/arm2/counterparties/' + parents['cp']))['primary_signer_id'] == reps[2]['id']
    assert len(list(db.scalars(select(CounterpartyRepresentative)))) == 3
    data = exported(c, parents['cp'], 'xlsx')
    parsed, _ = parse(data, 'v3.xlsx')
    assert len(parsed['representatives']) == 3
    assert all(not m['conflicts'] for m in parsed['representative_meta'])
    book = load_workbook(io.BytesIO(data), data_only=True)
    assert book[PASSPORT]['B20'].value == book[REPRESENTATIVES]['C7'].value == reps[2]['name']
    book.close()


def test_v3_export_never_turns_domain_text_into_excel_formulas(b5):
    c, _, _, parents, *_ = b5
    cp = check(c.get('/api/arm2/counterparties/' + parents['cp']))
    check(c.put('/api/arm2/counterparties/' + parents['cp'], json={
        'name': cp['name'], 'expected_version': cp['version'], 'short_name': '=1+1'}))
    check(c.post('/api/arm2/counterparties/' + parents['cp'] + '/representatives',
        json={'name': '=HYPERLINK("https://example.invalid","synthetic")'}), 201)
    data = exported(c, parents['cp'], 'xlsx')
    book = load_workbook(io.BytesIO(data))
    assert book[PASSPORT]['B12'].value == '=1+1' and book[PASSPORT]['B12'].data_type == 's'
    assert book[REPRESENTATIVES]['C5'].value.startswith('=HYPERLINK') and book[REPRESENTATIVES]['C5'].data_type == 's'
    book.close()


def test_v3_informational_access_uses_current_permissions_and_account_state(b5):
    c, db, _, parents, *_ = b5
    for i, (active, permissions) in enumerate([(True, []), (True, ['arm2_cp_view']), (False, ['arm2_cp_view'])]):
        user = User(login=f'synthetic-access-{i}', name='Synthetic access fixture',
            password_hash=hasher.hash('synthetic-test-only'), role='OPERATOR', active=active, permissions=permissions)
        db.add(user)
        db.commit()
        check(c.post('/api/arm2/counterparties/' + parents['cp'] + '/representatives',
                     json={'name': f'Synthetic contact {i}', 'user_id': user.id}), 201)
    book = load_workbook(io.BytesIO(exported(c, parents['cp'], 'xlsx')), data_only=True)
    assert [book[REPRESENTATIVES][f'I{row}'].value for row in (5, 6, 7)] == [
        'Нет доступа к АРМ; учётная запись активна', 'Есть доступ к АРМ', 'Учётная запись недоступна']
    book.close()


def test_v3_export_checks_expanded_ooxml_limit_not_only_compressed_xlsx_size(b5, monkeypatch):
    from app.arm2 import questionnaire_store
    c, _, _, parents, *_ = b5
    data = exported(c, parents['cp'], 'xlsx')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        expanded = sum(e.file_size for e in archive.infolist())
    assert expanded > len(data) * 2
    monkeypatch.setattr(questionnaire_store, 'MAX_EXPANDED', len(data) * 2)
    response = c.post('/api/arm2/questionnaires/exports/' + parents['cp'], json={'format': 'xlsx'})
    assert response.status_code == 422 and 'распакованного' in response.json()['detail']


def test_v3_unknown_row_status_is_a_row_error_and_does_not_block_unselected_other_fields(b5):
    c, _, _, parents, *_ = b5
    data = workbook({'B3': EXPORT_VERSION, 'B18': 'Accepted independently'},
                    rows=[{'C': 'UNSUPPORTED', 'I': 'A123BC86', 'J': 'Do not apply'}])
    draft = choose(c, upload(c, data), cp_target=parents['cp'], cp_fields=['phone'],
                   vehicles={'v:5': {'include': True, 'fields': ['name']}})
    assert draft['comparison']['vehicles'][0]['errors']
    apply(c, draft, 422)
    draft = choose(c, draft, vehicles={})
    receipt, _ = apply(c, draft)
    assert [o['kind'] for o in receipt['objects']] == ['CP']
    assert current(c, parents['veh'])['name'] == 'VEH'


@pytest.mark.parametrize('target', ['mailto:ignored@example.invalid', 'https://example.invalid/ignored'])
def test_legacy_inert_hyperlinks_are_discarded_without_network_or_payload_targets(target, monkeypatch):
    import socket

    from app.arm2.questionnaire_parse import safe_workbook

    def no_network(*args, **kwargs):
        raise AssertionError('Import must not navigate a hyperlink')
    monkeypatch.setattr(socket, 'create_connection', no_network)

    def extra(book):
        book[PASSPORT]['B19'].hyperlink = target
    data = workbook({'B19': 'cell@example.invalid'}, extra=extra)
    book, cached = safe_workbook(data)
    assert book[PASSPORT]['B19'].value == 'cell@example.invalid'
    book.close()
    cached.close()
    parsed, _ = parse(data, 'legacy.xlsx')
    assert parsed['cp']['email'] == 'cell@example.invalid' and target not in str(parsed)


@pytest.mark.parametrize('target', ['file:///C:/private', 'javascript:alert(1)', 'ftp://example.invalid/unsafe'])
def test_non_inert_hyperlink_schemes_remain_rejected(target):
    def extra(book):
        book[PASSPORT]['B19'].hyperlink = target
    with pytest.raises(ValueError, match='Внешние связи'):
        parse(workbook({'B19': 'cell@example.invalid'}, extra=extra), 'unsafe.xlsx')
