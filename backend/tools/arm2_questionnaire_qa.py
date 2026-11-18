"""AUTHOR-RUN B5 native browser -> pending DB/FILE/staging restore -> SAVE -> accepted restore.

Only creates new arm2_int_b5_*_test databases in the explicitly configured local QA
cluster. Reuses the existing B3 file-backed QA adapter and integration runner helpers.
Credentials, input ZIPs, dumps, runtime and logs remain in ignored .local.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from arm2_integration_qa import Server, file_manifest, migrate, port, request, require, run, snapshot, stage
from app.arm2.document_file_models import DocumentFile
from app.arm2.history_models import HistoryEvent
from app.auth import hasher
from app.models import User

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / 'tests'))
from test_arm2_questionnaire_workflow import transport, workbook  # noqa: E402


def picture(format, color, pages=1):
    image = Image.new('RGB', (300, 300), color)
    output = io.BytesIO()
    image.save(output, format, **({'save_all': True, 'append_images': [Image.new('RGB', (300, 300), 'white')]}
                                 if format == 'PDF' and pages == 2 else {}))
    return output.getvalue()


def tree_manifest(path):
    result = {}
    for item in path.rglob('*'):
        require(not item.is_symlink() and not (hasattr(item, 'is_junction') and item.is_junction()), 'Unsafe QA file tree')
        if item.is_file():
            result[item.relative_to(path).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def domain(url):
    return {k: v for k, v in snapshot(url).items() if not k.startswith('arm2_questionnaire_') and k != 'users'}


def frontend_start(server, directory):
    number = port()
    origin = f'http://127.0.0.1:{number}'
    server.env['APP_ORIGIN'] = origin
    server.start()
    log = (directory / 'frontend.log').open('ab')
    process = subprocess.Popen(['node', str(REPO / 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1',
        '--port', str(number), '--strictPort'], cwd=REPO / 'frontend', env={**os.environ, 'VITE_API_TARGET': server.base},
        stdout=log, stderr=log)
    import time

    import httpx
    for _ in range(100):
        try:
            if httpx.get(origin + '/arm2/questionnaires').status_code == 200:
                return process, log, origin
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    process.terminate()
    process.wait(20)
    log.close()
    raise RuntimeError('QA frontend did not start')


def browser(phase, state, directory):
    result = subprocess.run(['node', str(ROOT / 'tools/arm2_questionnaire_browser.mjs')],
        env={**os.environ, 'ARM2_B5_QA': json.dumps({**state, 'phase': phase, 'directory': str(directory)})}, capture_output=True)
    (directory / f'browser-{phase}.log').write_bytes(result.stdout + result.stderr)
    require(result.returncode == 0, f'Inspect native browser {phase} log in {directory}')
    return json.loads((directory / f'{phase}.json').read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pg-data', required=True)
    args = parser.parse_args()
    source = make_url(os.environ['TEST_DATABASE_URL'])
    require(source.host in ('127.0.0.1', 'localhost') and source.database.endswith('_test')
            and source.database != 'contractor_max_test', 'Only local disposable QA account allowed')
    pg_data = Path(args.pg_data).resolve()
    require(pg_data == REPO / '.local/pg-data', 'Only this clone own QA PostgreSQL can restart')
    pg_bin = Path(os.environ['ARM2_QA_PG_BIN'])
    work = REPO / '.local' / ('b5-native-' + uuid4().hex)
    work.mkdir(parents=True)
    provider = work / 'provider'
    provider.mkdir()
    import secrets
    password, signing = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    names = ['arm2_int_b5_' + uuid4().hex + suffix for suffix in ('_source_test', '_pending_test', '_accepted_test')]
    urls = [source.set(database=name) for name in names]
    admin = create_engine(source.set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as db:
        for name in names:
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin.dispose()
    migrate(urls[0], 'head')
    engine = create_engine(urls[0])
    with Session(engine) as db:
        for login in ('integration-qa', 'integration-rival'):
            db.add(User(login=login, name='Synthetic B5 ' + login, password_hash=hasher.hash(password),
                role='ADMIN_OPERATOR', active=True, permissions=[]))
        db.commit()
    engine.dispose()
    pg_env = {**os.environ, 'PGHOST': source.host, 'PGPORT': str(source.port), 'PGUSER': source.username,
              'PGPASSWORD': source.password or ''}
    server = Server(urls[0], work, provider, signing, port())
    frontend, frontend_log = None, None
    try:
        server.start()
        with server.login(password) as c:
            cp_a = request(c, 'POST', '/api/arm2/counterparties', 201, json={'name': 'Synthetic CP A'})
            cp = request(c, 'POST', '/api/arm2/counterparties', 201, json={'name': 'B5 Target CP', 'short_name': 'B5 Target CP',
                'full_name': 'Synthetic sole trader', 'legal_form': 'ИП', 'legal_address': 'Synthetic QA address',
                'inn': '1234567890', 'phone': 'Before questionnaire'})
            org = request(c, 'POST', '/api/arm2/organizations', 201, json={'name': 'Synthetic intervening ORG'})
            rep = request(c, 'POST', f"/api/arm2/counterparties/{cp['id']}/representatives", 201,
                json={'name': 'Synthetic B5 signer', 'position': 'Existing separate record'})
            typ = request(c, 'POST', '/api/arm2/types', 201, json={'name': 'B5 confirmed car', 'requires_sts': True,
                'requires_calibration': False, 'requires_wialon': False, 'plate_category': 'Автомобильный'})
            vehicle = request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Existing VEH', 'plate_number': 'A123BC86',
                'model': 'Before B5', 'type_id': typ['id'], 'owner_cp_id': cp_a['id']})
            archived = request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Archive declaration', 'plate_number': 'A125BC86',
                'type_id': typ['id'], 'accounting_state': 'Архивная'})
            unowned = request(c, 'POST', '/api/arm2/vehicles', 201, json={'name': 'Catalog only unowned', 'plate_number': 'A126BC86'})
            kinds = {}
            for name, parent in [('СТС', 'VEH'), ('Тарировка', 'VEH'), ('Карточка предприятия', 'CP')]:
                kinds[name] = request(c, 'POST', '/api/arm2/document-kinds', 201,
                    json={'name': name, 'applicable_object_types': [parent], 'max_current_documents': None,
                          'file_naming_template': 'PARENT_AND_KIND'})['id']
            base = f"/api/arm2/veh/{vehicle['id']}/documents"
            document = request(c, 'POST', base, 201, json={'kind_ref': kinds['СТС'], 'name': 'Existing registration set'})
            document_url = base + '/' + document['id']
            original_pdf, carried_png = picture('PDF', 'blue', 2), picture('PNG', 'purple')
            request(c, 'POST', document_url + '/accept', json={'expected_version': 1, 'idempotency_key': str(uuid4()),
                'files': [stage(c, document_url, original_pdf, 'historical-original.pdf'), stage(c, document_url, carried_png, 'carry-original.png')]})
            original_bundle = request(c, 'GET', document_url + '/files')
        server.stop()
        initial = domain(urls[0])
        new_pdf, company_pdf = picture('PDF', 'green', 2), picture('PDF', 'red')
        calibration_png, new_jpeg = picture('PNG', 'cyan'), picture('JPEG', 'yellow')
        nested = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(nested, 'w') as z:
            z.writestr('never-extract.txt', 'This nested ZIP is not a document')
        data = transport(workbook({'B4': '21 октября 2026г', 'B5': 'ИП', 'B6': cp['id'], 'B8': 'Synthetic', 'B9': 'B5',
            'B10': 'Fixture', 'B11': 'Synthetic sole trader', 'B12': cp['name'], 'B13': cp['inn'],
            'B18': 'Synthetic B5 contact', 'B20': 'Synthetic B5 signer', 'B21': 'Operator-selected signer',
            'B22': 'Synthetic authority', 'B24': '001234567', 'B25': '00000000000000000001', 'B27': 'Да', 'B28': 'company.pdf'},
            [{'B': vehicle['id'], 'C': 'ЗАЯВЛЕНО', 'D': typ['id'], 'E': vehicle['plate_number'], 'F': vehicle['name'],
              'G': 'B5 model', 'L': '37.5 — review only', 'P': 'updated-sts.pdf'},
             {'I': 'A124BC86', 'H': typ['id'], 'J': 'New from B5', 'K': 'New B5 model', 'N': 'calibration.png', 'P': 'new-sts.jpg'},
             {'B': archived['id'], 'D': typ['id'], 'E': archived['plate_number'], 'C': 'ЗАЯВЛЕНО', 'F': archived['name']},
             {'I': 'invalid', 'J': 'Do not create'}]),
            {'company.pdf': company_pdf, 'updated-sts.pdf': new_pdf, 'calibration.png': calibration_png,
             'new-sts.jpg': new_jpeg, 'unsupported.zip': nested.getvalue()})
        input_path = work / 'synthetic-questionnaire.zip'
        input_path.write_bytes(data)
        source_bytes = {hashlib.sha256(v).hexdigest(): v for v in (original_pdf, carried_png, new_pdf, company_pdf, calibration_png, new_jpeg)}
        state = {'password': password, 'cp': cp['id'], 'cp_a': cp_a['id'], 'org': org['id'], 'vehicle': vehicle['id'],
                 'archived': archived['id'], 'unowned': unowned['id'], 'type': typ['id'], 'original_rep': rep['id'],
                 'kinds': kinds, 'document': document['id'], 'document_url': document_url,
                 'replace_file': original_bundle['revisions'][0]['files'][0]['id'], 'input_path': str(input_path)}
        frontend, frontend_log, origin = frontend_start(server, work)
        review = browser('review', {**state, 'origin': origin}, work)
        state.update(review=review, draft=review['draft'])
        frontend.terminate()
        frontend.wait(20)
        frontend, _ = None, frontend_log.close()
        server.stop()
        after_review = domain(urls[0])
        # Only the explicitly competing B2 command is allowed to have changed before B5 SAVE.
        require(initial['arm2_counterparties'] == after_review['arm2_counterparties'], 'CP changed during review')
        require(initial['arm2_document_files'] == after_review['arm2_document_files'], 'FILE published during review')
        require(len(after_review['arm2_vehicles']) == 3 and len(after_review['arm2_history_events']) == len(initial['arm2_history_events']) + 1,
                'Review caused changes beyond the competing B2 command')
        pending_snapshot = snapshot(urls[0])
        pending_files = file_manifest(provider)
        runtime = work / 'runtime'
        pending_runtime = tree_manifest(runtime)
        run([pg_bin / 'pg_dump', '-Fc', '-f', work / 'pending.dump', names[0]], env=pg_env)
        pending_provider = work / 'pending-provider'
        pending_staging = work / 'pending-runtime'
        shutil.copytree(provider, pending_provider)
        shutil.copytree(runtime, pending_staging)
        run([pg_bin / 'pg_restore', '--exit-on-error', '--no-owner', '-d', names[1], work / 'pending.dump'], env=pg_env)
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-m', 'fast', 'stop'])
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-l', work / 'postgres-restart.log', 'start'])
        require(snapshot(urls[1]) == pending_snapshot and file_manifest(pending_provider) == pending_files
                and tree_manifest(pending_staging) == pending_runtime, 'Pending DB/FILE/private staging restore differs')
        server = Server(urls[1], work, pending_provider, signing, port())
        server.env['DATA_DIR'] = str(pending_staging)
        frontend, frontend_log, origin = frontend_start(server, work)
        accepted = browser('accept', {**state, 'origin': origin}, work)
        state.update(accepted=accepted)
        frontend.terminate()
        frontend.wait(20)
        frontend, _ = None, frontend_log.close()
        server.stop()
        accepted_snapshot = snapshot(urls[1])
        accepted_files = file_manifest(pending_provider)
        accepted_runtime = tree_manifest(pending_staging)
        run([pg_bin / 'pg_dump', '-Fc', '-f', work / 'accepted.dump', names[1]], env=pg_env)
        final_provider, final_runtime = work / 'accepted-provider', work / 'accepted-runtime'
        shutil.copytree(pending_provider, final_provider)
        shutil.copytree(pending_staging, final_runtime)
        run([pg_bin / 'pg_restore', '--exit-on-error', '--no-owner', '-d', names[2], work / 'accepted.dump'], env=pg_env)
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-m', 'fast', 'stop'])
        run([pg_bin / 'pg_ctl', '-D', pg_data, '-l', work / 'postgres-restart.log', 'start'])
        require(snapshot(urls[2]) == accepted_snapshot and file_manifest(final_provider) == accepted_files
                and tree_manifest(final_runtime) == accepted_runtime, 'Accepted DB/FILE/private staging restore differs')
        server = Server(urls[2], work, final_provider, signing, port())
        server.env['DATA_DIR'] = str(final_runtime)
        frontend, frontend_log, origin = frontend_start(server, work)
        final_ui = browser('reopen', {**state, 'origin': origin}, work)
        with server.login(password) as c:
            result_cp = request(c, 'GET', '/api/arm2/counterparties/' + cp['id'])
            require(result_cp['phone'] == 'Synthetic B5 contact' and result_cp['bik'] == '001234567'
                    and result_cp['settlement_account'] == '00000000000000000001', 'CP selected strings differ')
            require(result_cp['last_name'] == 'Synthetic' and result_cp['first_name'] == 'B5' and result_cp['middle_name'] == 'Fixture', 'Normalized CP names lost')
            reps = request(c, 'GET', f"/api/arm2/counterparties/{cp['id']}/representatives")
            require(len(reps) == 2 and result_cp['primary_signer_id'] != rep['id'] and all(r['user_id'] is None for r in reps), 'REP identity/user rights changed')
            vehicles = request(c, 'GET', '/api/arm2/vehicles/search?limit=100')['items']
            require(len(vehicles) == 4 and all(v['plate_number'] != 'invalid' for v in vehicles), 'Invalid/unselected row created')
            require(request(c, 'GET', '/api/arm2/vehicles/' + archived['id'])['accounting_state'] == 'Действующая', 'Archive transition not retained')
            current_bundle = request(c, 'GET', document_url + '/files')
            require(len(current_bundle['revisions']) == 2 and current_bundle['revisions'][1] == original_bundle['revisions'][0], 'Historical B3 revision changed')
            engine = create_engine(urls[2])
            with Session(engine) as db:
                file_rows = list(db.scalars(select(DocumentFile)))
                require(len(file_rows) == 6 and all(f.extension in ('pdf', 'jpg', 'png') for f in file_rows), 'Transport/nested ZIP became FILE')
                events = list(db.scalars(select(HistoryEvent).where(HistoryEvent.correlation_id == accepted['receipt']['command_id'], HistoryEvent.source == 'arm2-questionnaire')))
                require(len(events) == len(accepted['receipt']['event_ids']), 'Receipt/history correlation differs')
                head = db.scalar(text('SELECT version_num FROM alembic_version'))
                require(head == '0069', 'Expected single 0069 migration')
            engine.dispose()
            for row in file_rows:
                doc = next(json.loads(value) for value in accepted_snapshot['arm2_documents'] if json.loads(value)['id'] == row.document_id)
                revisions = request(c, 'GET', f"/api/arm2/{doc['parent_type'].lower()}/{doc['parent_id']}/documents/{doc['id']}/files")['revisions']
                revision = next(r for r in revisions if row.id in [f['id'] for f in r['files']])
                response = c.get(f"/api/arm2/{doc['parent_type'].lower()}/{doc['parent_id']}/documents/{doc['id']}/revisions/{revision['id']}/files/{row.id}/download")
                require(response.status_code == 200 and response.content == source_bytes[row.sha256], 'Restored current/historical bytes differ')
        report = {'classification': 'AUTHOR-RUN local synthetic QA; NOT Ubuntu UAT/independent acceptance',
            'base_sha': 'ddadebd7be7f5044628fd6b2ee656ca69d823db7', 'migration_head': head,
            'review': review, 'accepted': accepted, 'reopened': final_ui, 'restored_cp': result_cp,
            'pending_db_equal': True, 'pending_file_equal': True, 'pending_staging_equal': True,
            'accepted_db_equal': True, 'accepted_file_equal': True, 'accepted_staging_equal': True,
            'all_six_file_bytes_downloaded_after_restore': True, 'current_and_historical_file_ids_preserved': True,
            'postgres_backend_frontend_browser_restarted': True, 'file_manifest': accepted_files,
            'dump_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.glob('*.dump')},
            'pending_snapshot_hash': hashlib.sha256(json.dumps(pending_snapshot, sort_keys=True).encode()).hexdigest(),
            'accepted_snapshot_hash': hashlib.sha256(json.dumps(accepted_snapshot, sort_keys=True).encode()).hexdigest(),
            'databases': names, 'blockers': []}
        (work / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'result': 'PASS', 'evidence': str(work / 'result.json')}))
    finally:
        if frontend is not None:
            frontend.terminate()
            frontend.wait(20)
        if frontend_log is not None and not frontend_log.closed:
            frontend_log.close()
        server.stop()


if __name__ == '__main__':
    main()
