"""Own disposable B1 sandbox, never the installed ARM1/ARM2 runtime or secrets.

Prepare fresh vendor binaries/dependencies under .local/b1 first. No deployment.
"""
import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local/b1'
PG = LOCAL / 'runtime/pgsql/bin'
PYTHON = LOCAL / 'python/Scripts/python.exe'
DB_PORT, API_PORT, WEB_PORT = 55447, 18103, 18102
VISUAL_DB, TEST_DB = 'arm2_b1_visual', 'arm2_b1_test'
CONFIG = LOCAL / 'sandbox.json'
FLAGS = subprocess.CREATE_NO_WINDOW
sys.dont_write_bytecode = True


def allowed(method, path):
    if re.fullmatch(r'/api/arm2/(types|vehicles|counterparties|organizations)/search', path):
        return method == 'GET'
    from uat_policy import allowed as base_allowed

    return base_allowed(method, path)


def config():
    result = json.loads(CONFIG.read_text())
    if result['root'] != str(ROOT) or result['port'] != DB_PORT:
        raise RuntimeError('B1 sandbox identity mismatch')
    return result


def environment(database=VISUAL_DB):
    if database not in {VISUAL_DB, TEST_DB}:
        raise RuntimeError('Not a B1 database')
    cfg = config()
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES', 'PROGRAMFILES(X86)'}}
    env.update(APP_ENV='test', APP_ORIGIN=f'http://127.0.0.1:{WEB_PORT}', COOKIE_SECURE='false',
               ORGANIZATIONS_ENABLED='false', DATA_DIR=str(LOCAL / 'data'), MAX_BOT_TOKEN='',
               MAX_WEBHOOK_SECRET=cfg['inert_secret'], MAX_UPDATE_MODE='webhook', YANDEX_DISK_TOKEN='',
               YANDEX_MAPS_API_KEY='', ML_AUTO_TRAIN='false', ML_DOWNLOAD_BACKBONE='false',
               DATABASE_URL=f"postgresql+psycopg://b1:{cfg['db_password']}@127.0.0.1:{DB_PORT}/{database}",
               TEST_DATABASE_URL=f"postgresql+psycopg://b1:{cfg['db_password']}@127.0.0.1:{DB_PORT}/{TEST_DB}",
               PYTHONPATH=os.pathsep.join(map(str, [ROOT / 'backend', ROOT / 'ops', ROOT / 'deploy/arm2-windows-uat',
                                                   ROOT / 'deploy/arm2-windows-native'])),
               PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               VITE_API_TARGET=f'http://127.0.0.1:{API_PORT}', npm_config_cache=str(LOCAL / 'cache/npm'))
    env['PATH'] = os.pathsep.join([str(PG), str(PYTHON.parent), str(Path(os.environ['ProgramFiles']) / 'nodejs'), str(Path(os.environ['SystemRoot']) / 'System32')])
    return env


def guard():
    """Validate before any app import/pytest fixture can open a connection."""
    from urllib.parse import urlsplit

    url = urlsplit(os.environ.get('DATABASE_URL', ''))
    if (url.scheme, url.hostname, url.port, url.username, url.path) not in {
        ('postgresql+psycopg', '127.0.0.1', DB_PORT, 'b1', '/' + VISUAL_DB),
        ('postgresql+psycopg', '127.0.0.1', DB_PORT, 'b1', '/' + TEST_DB),
    } or url.query or url.fragment or url.password != config()['db_password']:
        raise RuntimeError('Only the own B1 literal-loopback DSN is accepted; details suppressed')
    if any(os.environ.get(k) != v for k, v in environment().items() if k in {
        'APP_ENV', 'APP_ORIGIN', 'COOKIE_SECURE', 'ORGANIZATIONS_ENABLED', 'DATA_DIR', 'MAX_BOT_TOKEN',
        'MAX_UPDATE_MODE', 'YANDEX_DISK_TOKEN', 'ML_AUTO_TRAIN', 'ML_DOWNLOAD_BACKBONE', 'TEST_DATABASE_URL'}):
        raise RuntimeError('B1 process inherited unsafe settings')
    # Same reviewed Python socket boundary as #42, configured ONLY in this process.
    import policy

    policy.DB_PORT, policy.API_PORT, policy.WEB_PORT = DB_PORT, API_PORT, WEB_PORT
    policy.deny_external_network()
    import psycopg

    with psycopg.connect(host='127.0.0.1', port=DB_PORT, user='b1', password=config()['db_password'],
                         dbname='postgres', connect_timeout=5) as db:
        if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != (LOCAL / 'pgdata').resolve():
            raise RuntimeError('Not the own B1 PostgreSQL cluster')


def run(args, database=VISUAL_DB, **kwargs):
    return subprocess.run(list(map(str, args)), env=environment(database), check=True, creationflags=FLAGS, **kwargs)


def pg_owned():
    import psutil

    path = LOCAL / 'pgdata/postmaster.pid'
    if not path.exists():
        return False
    rows = path.read_text().splitlines()
    process = psutil.Process(int(rows[0]))
    args = process.cmdline()
    if (Path(process.exe()).resolve() != (PG / 'postgres.exe').resolve() or rows[3] != str(DB_PORT)
            or Path(rows[1]).resolve() != (LOCAL / 'pgdata').resolve() or '-D' not in args
            or Path(args[args.index('-D') + 1]).resolve() != (LOCAL / 'pgdata').resolve()):
        raise RuntimeError('B1 PostgreSQL ownership mismatch')
    return True


def start_pg():
    if pg_owned():
        return
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', DB_PORT))
    run([PG / 'pg_ctl.exe', '-D', LOCAL / 'pgdata', '-l', LOCAL / 'postgres.log', '-w', '-t', '30',
         '-o', f'-h 127.0.0.1 -p {DB_PORT}', 'start'])
    assert pg_owned()


def init():
    if CONFIG.exists() or (LOCAL / 'pgdata').exists():
        raise RuntimeError('Refusing B1 reinitialization')
    for path in [ROOT, *ROOT.parents]:
        if path.is_symlink() or path.is_junction():
            raise RuntimeError('B1 root ancestor is a reparse point')
    for port in [DB_PORT, API_PORT, WEB_PORT, 18104]:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    for name in ['data', 'evidence']:
        (LOCAL / name).mkdir(exist_ok=True)
    cfg = {'root': str(ROOT), 'port': DB_PORT, 'db_password': secrets.token_hex(32),
           'admin_password': secrets.token_urlsafe(24), 'inert_secret': secrets.token_hex(32), 'nonce': secrets.token_hex(16)}
    with CONFIG.open('x', encoding='utf-8') as file:
        json.dump(cfg, file)
    script = "$p=$env:B1_CONFIG;$a=Get-Acl -LiteralPath $p;$a.SetAccessRuleProtection($true,$false);$u=[Security.Principal.WindowsIdentity]::GetCurrent().User;$a.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new($u,'FullControl','Allow'));Set-Acl -LiteralPath $p -AclObject $a"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   env=os.environ | {'B1_CONFIG': str(CONFIG)}, check=True, creationflags=FLAGS)
    password = LOCAL / 'init-password.txt'
    password.write_text(cfg['db_password'], encoding='ascii')
    try:
        run([PG / 'initdb.exe', '-D', LOCAL / 'pgdata', '-U', 'b1', '-A', 'scram-sha-256', '--pwfile', password,
             '--encoding=UTF8', '--locale=C'])
    finally:
        password.unlink(missing_ok=True)  # only our own short-lived file
    start_pg()
    run([PYTHON, __file__, 'migrate'], cwd=ROOT / 'backend')
    (LOCAL / 'processes.json').write_text('{}')
    print('Own B1 cluster, empty test DB and migrated synthetic visual DB ready')


def migrate():
    guard()
    import psycopg

    with psycopg.connect(host='127.0.0.1', port=DB_PORT, user='b1', password=config()['db_password'],
                         dbname='postgres', autocommit=True) as db:
        for name in [TEST_DB, VISUAL_DB]:
            assert not db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone()
            db.execute('CREATE DATABASE ' + name)
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / 'backend/alembic.ini')), '0061')
    import getpass

    from app.cli import main

    getpass.getpass = lambda *_a, **_k: config()['admin_password']
    sys.argv = ['app.cli', 'create-admin', '--login', 'b1-admin', '--name', 'B1 synthetic administrator']
    main()


def tests():
    with (LOCAL / 'backend-pytest.log').open('wb') as output:
        run([PYTHON, __file__, 'test_run'], database=TEST_DB, cwd=ROOT / 'backend', stdout=output, stderr=output)
    print((LOCAL / 'backend-pytest.log').read_text(encoding='utf-8')[-1800:])


def seed():
    run([PYTHON, ROOT / 'ops/arm2_b1_seed.py'], cwd=ROOT / 'backend')
    print('Own B1 visual fixtures prepared')


def baseline():
    import io
    import zipfile

    import psutil

    target = LOCAL / 'before'
    target.mkdir(exist_ok=False)
    archive = subprocess.check_output(['git', '-c', 'safe.directory=' + str(ROOT), '-C', str(ROOT),
                                       'archive', '--format=zip', '651098136b38a17cc56a4629d040b439d79f6431', 'frontend'])
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            path = (target / member.filename).resolve()
            assert path.is_relative_to(target.resolve())
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bundle.read(member))
    node = Path(os.environ['ProgramFiles']) / 'nodejs'
    with (LOCAL / 'baseline-setup.log').open('wb') as output:
        run([node / 'npm.cmd', 'ci', '--offline', '--no-audit', '--no-fund'], cwd=target / 'frontend', stdout=output, stderr=output)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 18104))
    with (LOCAL / 'before-web.log').open('ab', buffering=0) as output:
        process = subprocess.Popen(list(map(str, [node / 'node.exe', target / 'frontend/node_modules/vite/bin/vite.js',
                                   '--host', '127.0.0.1', '--port', '18104', '--strictPort'])),
                                   cwd=target / 'frontend', env=environment(), stdout=output, stderr=output, creationflags=FLAGS)
    state = json.loads((LOCAL / 'processes.json').read_text())
    state['before'] = {'pid': process.pid, 'created': psutil.Process(process.pid).create_time()}
    (LOCAL / 'processes.json').write_text(json.dumps(state))
    print('Own baseline frontend from exact #42, port 18104; same synthetic DB, no installed UAT used')


def test_run():
    guard()
    import pytest

    os.chdir(ROOT / 'backend')
    raise SystemExit(pytest.main(['tests', '-q', '--basetemp=' + str(LOCAL / 'pytest'), '-k', 'arm2']))


def start():
    import psutil

    start_pg()
    state = json.loads((LOCAL / 'processes.json').read_text())
    if state:
        raise RuntimeError('B1 processes already recorded; use scoped stop')
    commands = {'api': [PYTHON, '-B', __file__, 'api'],
                'web': [Path(os.environ['ProgramFiles']) / 'nodejs/node.exe', ROOT / 'frontend/node_modules/vite/bin/vite.js',
                        '--host', '127.0.0.1', '--port', str(WEB_PORT), '--strictPort']}
    for port in [API_PORT, WEB_PORT]:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    for name, command in commands.items():
        with (LOCAL / (name + '.log')).open('ab', buffering=0) as output:
            process = subprocess.Popen(list(map(str, command)), env=environment(), cwd=ROOT / ('frontend' if name == 'web' else 'backend'),
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output, creationflags=FLAGS)
        state[name] = {'pid': process.pid, 'created': psutil.Process(process.pid).create_time()}
        (LOCAL / 'processes.json').write_text(json.dumps(state))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            identity = json.load(opener.open(f'http://127.0.0.1:{API_PORT}/api/health', timeout=2))
            opener.open(f'http://127.0.0.1:{WEB_PORT}', timeout=2).close()
            assert identity['nonce'] == config()['nonce']
            print('B1 synthetic sandbox ready at 127.0.0.1:' + str(WEB_PORT))
            return
        except OSError:
            time.sleep(.2)
    raise RuntimeError('B1 readiness failed; inspect only own logs')


def stop():
    import psutil

    statefile = LOCAL / 'processes.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    for value in state.values():
        try:
            process = psutil.Process(value['pid'])
        except psutil.NoSuchProcess:
            continue
        if abs(process.create_time() - value['created']) > .01:
            raise RuntimeError('Reused B1 PID; refusing stop')
        def owned(p):
            return str(ROOT).casefold().replace('\\', '/') in ' '.join(p.cmdline()).casefold().replace('\\', '/')
        if not owned(process):
            raise RuntimeError('Unknown B1 process')
        children = process.children(recursive=True)
        targets = []
        for child in children:
            if owned(child):
                targets.append(child)
            elif not (Path(child.exe()).resolve() == (Path(os.environ['SystemRoot']) / 'System32/conhost.exe').resolve()
                      and child.ppid() == process.pid):
                raise RuntimeError('Unknown B1 child; refusing broad shutdown')
        for child in reversed(targets):
            child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs([process, *children], timeout=10)
        if alive:
            raise RuntimeError('Own B1 process did not stop')
    statefile.write_text('{}')
    if pg_owned():
        run([PG / 'pg_ctl.exe', '-D', LOCAL / 'pgdata', '-m', 'fast', '-w', '-t', '30', 'stop'])
    print('Only own B1 processes stopped; own synthetic data retained')


def api():
    guard()
    import uvicorn

    uvicorn.run('arm2_b1_app:app', host='127.0.0.1', port=API_PORT, access_log=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'migrate', 'tests', 'test_run', 'seed', 'baseline', 'start', 'stop', 'api'])
    globals()[parser.parse_args().action]()
