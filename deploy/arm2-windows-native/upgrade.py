"""Fail-closed B1 upgrade gates. Preflight is read-only on the Windows instance.

Never initialize a database or infer a destination from environment variables.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path('C:/ARM2-UAT/ARM2-WIN-RUN-001')
B1 = 'aaba19e5c363a8fee22eb6c49060d3452117bcd9'
BASE = '651098136b38a17cc56a4629d040b439d79f6431'
OLD = '6118252e8a43e8bf534012ba91284c20427ea5e0'
BUSINESS = '1f8c06d1f9269146ce7c07508b02dd09c89ff763'
BRANCH = 'codex/arm2-win-b1-uat-001'
ARM1 = Path('C:/ARM/CONTRACTOR_MAX/transport-max')
REPO = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def git(repo, *args, binary=False):
    return subprocess.check_output(['git', '--no-optional-locks', '-c', f'safe.directory={repo.as_posix()}',
                                    '-C', str(repo), *args], text=not binary).strip()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def old_controller():
    # Import only the already installed controller. No app import or migration.
    native = ROOT / 'source/deploy/arm2-windows-native'
    sys.path.insert(0, str(native))
    spec = importlib.util.spec_from_file_location('installed_controller', native / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.require_root()
    return module


def db_snapshot(connection):
    """All public tables: content digests only, never publish records or secrets."""
    from psycopg import sql

    result = {}
    tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    for (table,) in tables:
        rows = connection.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} AS t').format(sql.Identifier(table))).fetchall()
        hashes = sorted(hashlib.sha256(row[0].encode()).hexdigest() for row in rows)
        result[table] = {'rows': len(rows), 'sha256': hashlib.sha256(''.join(hashes).encode()).hexdigest()}
    return result


def protected_files():
    result = {}
    # Code and fixed configuration; do not hash a running PostgreSQL data directory.
    for rel in git(ARM1, 'ls-files').splitlines():
        path = ARM1 / rel
        if path.is_file():
            result[rel] = sha(path)
    for rel in ['START_CONTRACTOR.cmd', '.local/config.json', '.local/postgres-data/postgresql.conf',
                '.local/postgres-data/pg_hba.conf']:
        path = ARM1 / rel
        if path.is_file():
            result[rel] = sha(path)
    return result


def preflight():
    import psutil
    import psycopg

    require(ROOT.is_dir() and ROOT.resolve() == ROOT, 'UAT root missing/redirected')
    for path in [ROOT, *ROOT.parents]:
        require(not path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT, 'Reparse ancestor')
    require(shutil.disk_usage(ROOT).free > 10 * 1024**3, 'Less than 10 GiB free')
    require(git(REPO, 'merge-base', 'HEAD', B1) == B1, 'Not a child of exact B1')
    require(git(REPO, 'branch', '--show-current') == BRANCH, 'Unexpected preparation branch')
    source = ROOT / 'source'
    require(git(source, 'rev-parse', 'HEAD') == OLD, 'Installed source HEAD changed')
    require(git(source, 'branch', '--show-current') == 'codex/arm-win-struct-001', 'Installed branch changed')
    require(git(source, 'status', '--porcelain', '--untracked-files=normal') == '?? deploy/arm2-windows-native/',
            'Unexpected installed source changes')
    # Installed wrapper originally shipped outside that checkout. Match #42 bytes.
    wrapper = source / 'deploy/arm2-windows-native'
    for path in wrapper.iterdir():
        if path.name == '__pycache__' and path.is_dir():
            continue
        require(path.is_file(), 'Unexpected installed wrapper directory')
        expected = git(REPO, 'show', f'{BASE}:deploy/arm2-windows-native/{path.name}', binary=True)
        require(path.read_bytes().replace(b'\r\n', b'\n').rstrip() == expected.replace(b'\r\n', b'\n'),
                'Installed wrapper differs from approved #42: ' + path.name)
    identity = read(ROOT / 'private/instance.json')
    require(identity['root'] == str(ROOT) and identity['package'] == 'ARM2-WIN-RUN-001'
            and identity['approved_head'] == OLD, 'Installed metadata changed')
    controller = old_controller()
    require(controller.pg_owned(), 'Own PostgreSQL not running')
    state = read(controller.STATE)
    owners = {}
    for role in ('web', 'api'):
        proc = controller.owned_process(state[role], role)
        require(proc is not None, 'Own UAT role stopped')
        require(Path(proc.exe()).resolve() == (ROOT / 'python/Scripts/python.exe').resolve()
                or str(ROOT / 'python') in proc.exe(), 'Role executable outside UAT')
        owners[role] = {'pid': proc.pid, 'created': proc.create_time()}
    listeners = []
    for c in psutil.net_connections(kind='tcp'):
        if c.status != 'LISTEN' or c.laddr.port not in {5174, 8014, 55432, 18082, 18083, 55442}:
            continue
        process = psutil.Process(c.pid)
        listeners.append({'port': c.laddr.port, 'address': c.laddr.ip, 'pid': c.pid,
                          'created': process.create_time(), 'exe': process.exe()})
        if c.laddr.port in {18082, 18083, 55442}:
            require(c.laddr.ip == '127.0.0.1', 'Non-loopback UAT listener')
            if c.laddr.port == 55442:
                require(Path(process.exe()).resolve() == ROOT / 'runtime/pgsql/bin/postgres.exe', 'Foreign PostgreSQL')
            else:
                role = 'api' if c.laddr.port == 18083 else 'web'
                own_pid = owners[role]['pid']
                require(process.pid == own_pid or own_pid in [p.pid for p in process.parents()], 'Foreign API/web process tree')
                require(str(controller.HERE / 'run.py') in process.cmdline() and process.cmdline()[-1] == role,
                        'Foreign API/web listener command')
    require({i['port'] for i in listeners} >= {18082, 18083, 55442}, 'UAT listeners missing')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:18082/api/health', timeout=5) as response:
        health = json.load(response)
    require(health.get('source_sha') == BUSINESS and health.get('approved_head') == OLD
            and health.get('instance_id') == identity['instance_id'] and health.get('migration') == '0061', 'Old health mismatch')
    private = read(ROOT / 'private/secrets.json')
    with psycopg.connect(host='127.0.0.1', port=55442, user='arm2_uat', password=private['db_password'],
                         dbname='arm2_win_run_001', connect_timeout=5, options='-c default_transaction_read_only=on') as db:
        require(Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() == ROOT / 'db-data', 'Wrong PGDATA')
        require(db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0061', 'Wrong schema')
        tables = db_snapshot(db)
    return {'result': 'READ_ONLY_PREFLIGHT_PASS', 'root': str(ROOT), 'health': health,
            'listeners': listeners, 'processes': owners, 'tables': tables,
            'protected_files': protected_files(), 'private_sha': sha(ROOT / 'private/secrets.json'),
            'free_bytes': shutil.disk_usage(ROOT).free,
            'dsn': 'postgresql+psycopg://arm2_uat:***@127.0.0.1:55442/arm2_win_run_001'}


def write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def private_env():
    secret = read(ROOT / 'private/secrets.json')
    clean = {k: v for k, v in os.environ.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT'}}
    clean.update(PGPASSWORD=secret['db_password'], PGCONNECT_TIMEOUT='5',
                 PATH=str(ROOT / 'runtime/pgsql/bin') + os.pathsep + str(Path(os.environ['SystemRoot']) / 'System32'),
                 PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    return clean


def execute(args, *, cwd=None):
    # Capture diagnostics locally; exceptions never include a password/connection URL.
    result = subprocess.run(list(map(str, args)), env=private_env(), cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('UAT command failed: ' + Path(args[0]).name + ' (inspect private upgrade log)')
    return result.stdout


def controller(action):
    return execute([ROOT / 'tools/bootstrap/Scripts/python.exe', '-B', '-X', 'utf8',
                    ROOT / 'source/deploy/arm2-windows-native/run.py', action])


def connection(port=55442, database='arm2_win_run_001', directory=None):
    import psycopg

    require(port in {55442, 55448}, 'Foreign backup/drill port')
    db = psycopg.connect(host='127.0.0.1', port=port, user='arm2_uat',
                         password=read(ROOT / 'private/secrets.json')['db_password'],
                         dbname=database, connect_timeout=5, autocommit=True)
    expected = directory if directory is not None else ROOT / 'db-data'
    if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != expected.resolve():
        db.close()
        raise RuntimeError('Wrong backup/drill PostgreSQL directory')
    return db


def source_copy(source, target):
    # No DB/runtime/secret tree is a source. All code + exact Git identity + built UI retained.
    require(source.resolve().name in {'source', 'arm2-win-b1-uat-001'}, 'Unexpected source copy')
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('node_modules', '__pycache__', '.local', '.ruff_cache'))


def backup_and_drill(folder, snapshot):
    """A new scratch cluster only; never restore into the existing UAT database."""
    import socket

    pg = ROOT / 'runtime/pgsql/bin'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 55448))
    dump = folder / 'uat.dump'
    execute([pg / 'pg_dump.exe', '-h', '127.0.0.1', '-p', '55442', '-U', 'arm2_uat',
             '-d', 'arm2_win_run_001', '--format=custom', '--no-owner', '--no-privileges', '-f', dump])
    require(dump.stat().st_size > 0, 'Empty backup')
    execute([pg / 'pg_restore.exe', '--list', dump])
    data = folder / 'drill-data'
    pw = folder / 'drill-password.txt'
    pw.write_text(read(ROOT / 'private/secrets.json')['db_password'] + '\n', encoding='ascii')
    try:
        execute([pg / 'initdb.exe', '-D', data, '-U', 'arm2_uat', '-A', 'scram-sha-256',
                 '--pwfile', pw, '--encoding=UTF8', '--locale=C'])
    finally:
        pw.unlink(missing_ok=True)
    execute([pg / 'pg_ctl.exe', '-D', data, '-l', folder / 'drill.log', '-w', '-t', '30',
             '-o', '-h 127.0.0.1 -p 55448', 'start'])
    try:
        with connection(55448, 'postgres', data) as db:
            db.execute('CREATE DATABASE arm2_win_run_001')
        execute([pg / 'pg_restore.exe', '-h', '127.0.0.1', '-p', '55448', '-U', 'arm2_uat',
                 '-d', 'arm2_win_run_001', '--exit-on-error', '--no-owner', '--no-privileges', dump])
        with connection(55448, directory=data) as db:
            require(db_snapshot(db) == snapshot, 'Restored backup differs from UAT snapshot')
        write(folder / 'backup-verified.json', {'result': 'RESTORE_DRILL_PASS', 'sha256': sha(dump),
                                               'tables': snapshot, 'port': 55448, 'directory': str(data)})
    finally:
        # Verify the exact scratch server before using scoped pg_ctl. No kill by name/port.
        with connection(55448, 'postgres', data):
            pass
        execute([pg / 'pg_ctl.exe', '-D', data, '-m', 'fast', '-w', '-t', '30', 'stop'])


def prepare():
    before = preflight()  # No installed write before this gate.
    require(not git(REPO, 'status', '--porcelain'), 'Commit reviewed preparation first')
    head = git(REPO, 'rev-parse', 'HEAD')
    require((REPO / 'frontend/dist/index.html').is_file(), 'Build B1 frontend before preparation')
    folder = ROOT / 'private' / ('b1-upgrade-' + head[:12])
    require(not folder.exists(), 'Upgrade already prepared; do not overwrite backups')
    folder.mkdir()  # Inherit owner-only private ACL; no platform/system change.
    write(folder / 'preflight.json', before)
    for name in ['instance.json', 'secrets.json', 'processes.json']:
        shutil.copy2(ROOT / 'private' / name, folder / name)
    source_copy(ROOT / 'source', folder / 'source-original')
    source_copy(REPO, folder / 'source-staged')
    # Original Git index + native wrapper + built UI copied before any installation mutation.
    original_files = {p.relative_to(folder / 'source-original').as_posix(): sha(p)
                      for p in (folder / 'source-original').rglob('*') if p.is_file()}
    staged = folder / 'source-staged'
    staged_files = {p.relative_to(staged).as_posix(): sha(p) for p in staged.rglob('*') if p.is_file()}
    write(folder / 'source-copies.json', {'original': original_files, 'staged': staged_files})
    # Online consistent dump. Recheck the complete read model to detect concurrent operator edits.
    backup_and_drill(folder, before['tables'])
    require(preflight()['tables'] == before['tables'], 'Operator changed UAT during backup; NO-GO')
    write(folder / 'prepared.json', {'package_head': head, 'source_sha': B1, 'instance_id': before['health']['instance_id']})
    print(json.dumps({'result': 'PREPARED_BACKUP_DRILL_PASS', 'folder': str(folder)}))


def prepared_folder(head=None):
    head = head or git(REPO, 'rev-parse', 'HEAD')
    require(len(head) == 40 and all(c in '0123456789abcdef' for c in head), 'Invalid release SHA')
    folder = ROOT / 'private' / ('b1-upgrade-' + head[:12])
    meta = read(folder / 'prepared.json')
    require(meta['package_head'] == head and meta['source_sha'] == B1, 'Prepared release mismatch')
    require(read(folder / 'backup-verified.json')['sha256'] == sha(folder / 'uat.dump'), 'Backup hash changed')
    return folder, meta


def replace_json(path, value):
    temporary = path.with_suffix('.b1-tmp')
    write(temporary, value)
    temporary.replace(path)


def install():
    folder, meta = prepared_folder()
    before = read(folder / 'preflight.json')
    current = preflight()
    require(current['protected_files'] == before['protected_files'] and current['private_sha'] == before['private_sha'],
            'Protected baseline changed')
    require(current['tables'] == before['tables'], 'Operator changed UAT after backup; prepare new backup')
    copies = read(folder / 'source-copies.json')
    for relative, expected in copies['staged'].items():
        require(sha(folder / 'source-staged' / relative) == expected, 'Staged release changed')
    controller('stop')  # Existing controller checks PID/create_time/cmd and exact own PGDATA.
    swapped = False
    try:
        # Stop provides a write barrier. Existing DB remains in exactly the same directory.
        (ROOT / 'source').rename(folder / 'source-stopped')
        (folder / 'source-staged').rename(ROOT / 'source')
        swapped = True
        identity = read(folder / 'instance.json')
        identity.update(approved_head=B1, origin_head=OLD, source_sha=B1)
        files = {k: v for k, v in copies['staged'].items() if not k.startswith('.git/')}
        replace_json(ROOT / 'private/release.json', meta | {'files': files})
        replace_json(ROOT / 'private/instance.json', identity)
        controller('start')
        with connection() as db:
            require(db_snapshot(db) == before['tables'], 'Existing records changed during upgrade')
        require(protected_files() == before['protected_files'], 'ARM1 files changed')
        require(sha(ROOT / 'private/secrets.json') == before['private_sha'], 'Instance secrets changed')
        write(folder / 'installed.json', {'result': 'INSTALLED_PENDING_SMOKE', 'package_head': meta['package_head']})
        print('INSTALLED_PENDING_SMOKE')
    except BaseException:
        if swapped:
            rollback(folder, meta)
        else:
            if not (ROOT / 'source').exists() and (folder / 'source-stopped').exists():
                (folder / 'source-stopped').rename(ROOT / 'source')
            controller('start')
        raise


def rollback(folder, meta):
    """Return code/config only. DB restore requires separate, verified incident decision.

    No migrations run, so reverting code normally leaves the unchanged database intact.
    Original data dump + independent restore drill remain in the private folder.
    """
    require(folder.parent == ROOT / 'private', 'Foreign rollback path')
    require(read(folder / 'prepared.json') == meta, 'Rollback identity mismatch')
    require(sha(ROOT / 'private/secrets.json') == sha(folder / 'secrets.json'), 'Secrets changed; manual review required')
    controller('stop')
    (ROOT / 'source').rename(folder / ('source-failed-' + str(time.time_ns())))
    (folder / 'source-stopped').rename(ROOT / 'source')
    replace_json(ROOT / 'private/instance.json', read(folder / 'instance.json'))
    if (ROOT / 'private/release.json').exists():
        (ROOT / 'private/release.json').rename(folder / ('release-failed-' + str(time.time_ns()) + '.json'))
    controller('start')
    write(folder / 'rollback.json', {'result': 'FAILED+ROLLBACK', 'restored_head': OLD})
    print('FAILED+ROLLBACK: old code/health restored; database retained, dump available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'prepare', 'install', 'rollback'])
    action = parser.parse_args().action
    if action == 'preflight':
        print(json.dumps(preflight(), ensure_ascii=False, indent=2))
    elif action == 'rollback':
        rollback(*prepared_folder(read(ROOT / 'private/release.json')['package_head']))
    else:
        globals()[action]()


if __name__ == '__main__':
    main()
