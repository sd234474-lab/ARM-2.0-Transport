"""Issue 72 author smoke on a NEW local disposable DB/FILE, never VM/UAT.

TEST_DATABASE_URL is only a local QA cluster account. Its database is never changed.
Uses the existing integration Server, B5 transport helpers and pinned native browser.
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

from arm2_integration_qa import Server, migrate, port, request, require, stage
from arm2_questionnaire_qa import frontend_start, picture, snapshot, transport, workbook
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.arm2.counterparty_rules import LEGAL
from app.arm2.document_file_models import DocumentFile
from app.arm2.history_models import HistoryEvent
from app.arm2.uat_reset import reset
from app.auth import hasher
from app.config import settings
from app.models import User

REPO = Path(__file__).resolve().parents[2]


def run_browser(phase, state, work):
    result = subprocess.run(['node', str(Path(__file__).with_name('arm2_issue72_browser.mjs'))],
        env={**os.environ, 'ARM2_ISSUE72': json.dumps({**state, 'phase': phase, 'directory': str(work)})},
        capture_output=True)
    (work / (phase + '.log')).write_bytes(result.stdout + result.stderr)
    require(result.returncode == 0, 'Native browser failed; inspect private smoke log: ' + str(work / (phase + '.log')))
    return json.loads((work / (phase + '.json')).read_text(encoding='utf-8'))


def fixture(c, work):
    cp = request(c, 'POST', '/api/arm2/counterparties', 201, json={'type': LEGAL, 'name': 'Smoke CP retained',
        'full_name': 'Synthetic legal full', 'short_name': 'Synthetic short', 'legal_form': 'АО', 'phone': 'Before',
        'bik': '001234567', 'settlement_account': '00000000000000000001'})
    org = request(c, 'POST', '/api/arm2/organizations', 201, json={'name': 'Synthetic smoke ORG'})
    rep = request(c, 'POST', f"/api/arm2/counterparties/{cp['id']}/representatives", 201,
        json={'name': 'Synthetic signer', 'position': 'Before'})
    cp = request(c, 'GET', '/api/arm2/counterparties/' + cp['id'])
    cp = request(c, 'PUT', '/api/arm2/counterparties/' + cp['id'],
        json={'expected_version': cp['version'], 'name': cp['name'], 'primary_signer_id': rep['id']})
    typ = request(c, 'POST', '/api/arm2/types', 201, json={'name': 'Confirmed smoke TYPE', 'requires_sts': True,
        'requires_calibration': False, 'requires_wialon': False, 'plate_category': 'Автомобильный'})
    vehicle = request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Smoke existing VEH', 'plate_number': 'A123BC86',
        'type_id': typ['id'], 'owner_cp_id': cp['id'], 'model': 'Before'})
    request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Smoke unowned active', 'plate_number': 'A124BC86'})
    request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Smoke unowned archived', 'plate_number': 'A125BC86',
        'accounting_state': 'Архивная', 'wialon_unit_id': 'synthetic-unit'})
    kind = request(c, 'POST', '/api/arm2/document-kinds', 201, json={'name': 'Карточка предприятия',
        'applicable_object_types': ['CP'], 'max_current_documents': None, 'file_naming_template': 'PARENT_AND_KIND'})
    base = f"/api/arm2/cp/{cp['id']}/documents"
    document = request(c, 'POST', base, 201, json={'kind_ref': kind['id'], 'name': 'Synthetic company original'})
    document_url = base + '/' + document['id']
    original, proposed = picture('PDF', 'blue', 2), picture('PDF', 'green', 2)
    request(c, 'POST', document_url + '/accept', json={'expected_version': 1, 'idempotency_key': str(uuid4()),
        'files': [stage(c, document_url, original, 'historical-original.pdf')]})
    bundle = request(c, 'GET', document_url + '/files')

    def representatives(book):
        sheet = book.create_sheet('Представители')
        for col, value in {'B': rep['id'], 'C': rep['name'], 'D': 'After questionnaire', 'H': 'Действующий', 'K': 'ДА'}.items():
            sheet[f'{col}5'] = value

    data = transport(workbook({'B3': 'ARM_IMPORT_V4', 'B5': LEGAL, 'B6': cp['id'], 'B7': 'АО',
        'B12': cp['short_name'], 'B18': 'After questionnaire', 'B20': rep['name'], 'B21': 'After questionnaire',
        'B24': '001234567', 'B25': '00000000000000000002', 'B28': 'company-replacement.pdf'},
        [{'B': vehicle['id'], 'C': 'ИЗ СПРАВОЧНИКА', 'D': typ['id'], 'E': vehicle['plate_number'],
          'F': vehicle['name'], 'G': 'After questionnaire'}], representatives), {'company-replacement.pdf': proposed})
    (work / 'synthetic-v4.zip').write_bytes(data)
    (work / 'fresh-v4.xlsx').write_bytes(workbook({'B3': 'ARM_IMPORT_V4', 'B5': LEGAL, 'B7': 'МУП', 'B12': 'After reset CP'}))
    return {'cp': cp['id'], 'org': org['id'], 'rep': rep['id'], 'vehicle': vehicle['id'], 'kind': kind['id'],
        'document': document['id'], 'document_url': document_url, 'replace_file': bundle['revisions'][0]['files'][0]['id'],
        'input_path': str(work / 'synthetic-v4.zip'), 'fresh_path': str(work / 'fresh-v4.xlsx'),
        'original_sha': hashlib.sha256(original).hexdigest(), 'proposed_sha': hashlib.sha256(proposed).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = make_url(os.environ['TEST_DATABASE_URL'])
    require(source.host in ('127.0.0.1', 'localhost') and source.database.endswith('_test')
            and source.database != 'contractor_max_test', 'Only explicitly disposable local QA account allowed')
    name = 'arm2_int_issue72_' + uuid4().hex + '_test'
    url = source.set(database=name)
    with create_engine(source.set(database='postgres'), isolation_level='AUTOCOMMIT').connect() as db:
        require(not db.scalar(text('SELECT 1 FROM pg_database WHERE datname=:n'), {'n': name}), 'Database exists')
        db.exec_driver_sql('CREATE DATABASE "' + name + '"')
    work = REPO / '.local' / ('issue72-smoke-' + uuid4().hex)
    work.mkdir(parents=True)
    provider = work / 'provider'
    provider.mkdir()
    password, signing = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    os.environ.update({'APP_ENV': 'test', 'COOKIE_SECURE': 'false', 'MAX_BOT_TOKEN': '',
                       'YANDEX_DISK_TOKEN': '', 'YANDEX_MAPS_API_KEY': '', 'ORGANIZATIONS_ENABLED': 'false'})
    migrate(url, 'head')
    sql = create_engine(url)
    with Session(sql) as db:
        db.add(User(login='integration-qa', name='Synthetic Issue72 author', role='ADMIN_OPERATOR', active=True,
                    permissions=[], password_hash=hasher.hash(password)))
        db.commit()
    server = Server(url, work, provider, signing, port())
    frontend = frontend_log = None
    try:
        frontend, frontend_log, origin = frontend_start(server, work)
        with server.login(password) as c:
            state = fixture(c, work)
        private = {**state, 'origin': origin, 'password': password}
        (work / 'private-state.json').write_text(json.dumps(private), encoding='utf-8')
        core = run_browser('core', private, work)
        with server.login(password) as c:
            cp = request(c, 'GET', '/api/arm2/counterparties/' + state['cp'])
            require(cp['phone'] == 'After questionnaire' and cp['name'] == 'Smoke CP retained'
                    and cp['settlement_account'] == '00000000000000000002', 'CP readback differs')
            reps = request(c, 'GET', f"/api/arm2/counterparties/{state['cp']}/representatives")
            require(len(reps) == 1 and reps[0]['id'] == state['rep'] and reps[0]['position'] == 'After questionnaire'
                    and reps[0]['user_id'] is None and cp['primary_signer_id'] == state['rep'], 'REP identity/access differs')
            require(request(c, 'GET', '/api/arm2/vehicles/' + state['vehicle'])['model'] == 'After questionnaire', 'VEH differs')
            bundle = request(c, 'GET', state['document_url'] + '/files')
            hashes = []
            for revision in bundle['revisions']:
                for item in revision['files']:
                    response = c.get(state['document_url'] + f"/revisions/{revision['id']}/files/{item['id']}/download")
                    require(response.status_code == 200, 'Original unavailable')
                    hashes.append(hashlib.sha256(response.content).hexdigest())
            require(set(hashes) == {state['original_sha'], state['proposed_sha']}, 'Original bytes differ')
        with Session(sql) as db:
            events = list(db.scalars(select(HistoryEvent).where(HistoryEvent.correlation_id == core['receipt']['command_id'])))
            require({e.object_type for e in events} == {'CP', 'CPREP', 'VEH', 'DOCUMENT'}, 'Missing atomic object history')
            require(len(db.scalars(select(DocumentFile)).all()) == 2, 'Missing/duplicate FILE')
        before_reset = snapshot(url)
        require(any(before_reset[n] for n in before_reset if n.startswith('arm2_')), 'Reset fixture is empty')
        # Stop only this subprocess. No VM, Docker, shared cluster restart or live reset.
        server.stop()
        os.environ.update({'DATA_DIR': server.env['DATA_DIR'], 'APP_ENV': 'test', 'COOKIE_SECURE': 'false'})
        settings.cache_clear()
        attestation = {'project': 'arm2-ubuntu-uat-r2', 'file_volume': 'arm2-ubuntu-uat-r2_r2_file_data',
                       'data_volume': 'arm2-ubuntu-uat-r2_r2_photo_data', 'offline': True}
        results = [reset(sql, attestation, provider, Path(server.env['DATA_DIR']), execute=True) for _ in range(2)]
        require(all(not any(r['counts'].values()) and r['bootstrap_preserved'] for r in results), 'Reset incomplete')
        server.start()
        fresh = run_browser('fresh', private, work)
        with server.login(password) as c:
            result = request(c, 'GET', '/api/arm2/counterparties/' + fresh['cp_id'])
            require(result['type'] == LEGAL and result['legal_form'] == 'МУП', 'Fresh V4 import differs')
        report = {'classification': 'AUTHOR synthetic disposable local smoke; not UAT/acceptance',
            'base_sha': '7ce6e7d99b193ab9d35cf29db73c28dd8f30c43d', 'migration': '0070',
            'smoke': {f'I0{i}': {'result': 'PASS', 'evidence': evidence} for i, evidence in enumerate([
                'Native CP type+name/FIO creation and derived names',
                'Native type/OPF transition, visible clear choices, one SAVE, unchanged name/ID/bank, HIST',
                'V4 ZIP CP/REP/VEH/DOC apply; originals/hash/history readback; V2/V3 in focused tests',
                'Native active upload dedup, V4 roundtrip no-op; cancel in focused tests',
                'Active default, ALL reload, 2 SQL counters, empty search and mobile navigation',
                'Offline reset twice on populated disposable DB/FILE, preserved admin login, fresh native V4 import',
            ], 1)}, 'browser': core, 'fresh_import': fresh, 'file_hashes': hashes,
            'atomic_history_events': len(events), 'reset_counts': results[-1]['counts'], 'live_stand_modified': False}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('I01–I06 disposable smoke PASS; private artifacts: ' + str(work))
    finally:
        server.stop()
        if frontend:
            frontend.terminate()
            frontend.wait(20)
        if frontend_log:
            frontend_log.close()
        sql.dispose()


if __name__ == '__main__':
    main()
