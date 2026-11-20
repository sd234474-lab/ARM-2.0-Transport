"""Read-only R2 gate. No create/start/stop/migrate/reset, no secret-bearing output."""
import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path

PROJECT = 'arm2-ubuntu-uat-r2'
ROOT = Path('/home/arm2admin/arm2-uat/r2-release')
BASE = '3bd71a13bc92e5a048002fda0cfd23b8d5b1c720'
SERVICES = {'r2-db', 'r2-api', 'r2-web'}
VOLUMES = {n: PROJECT + '_' + n for n in ('r2_postgres_data', 'r2_photo_data', 'r2_file_data')}
NETWORKS = {n: PROJECT + '_' + n for n in ('r2_internal', 'r2_ingress')}
MIN_RAM = 2 * 1024**3
MIN_DISK = 15 * 1024**3


class NoGo(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise NoGo(reason)


def read_env(path):
    require(path.is_file() and not path.is_symlink(), 'env: regular private file required')
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, 'env: chmod 0600 required')
    require(path.resolve().parent != ROOT and ROOT not in path.resolve().parents, 'env: keep outside checkout')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        require(bool(re.fullmatch(r'[A-Z][A-Z0-9_]*=[^\s\x00]*', line)), 'env: plain KEY=value only')
        key, value = line.split('=', 1)
        require(key not in values, 'env: duplicate key')
        values[key] = value
    validate_env(values)
    return values


def validate_env(e):
    fixed = {'APP_ENV': 'test', 'ORGANIZATIONS_ENABLED': 'false', 'DATA_DIR': '/data',
             'COOKIE_SECURE': 'false', 'MAX_BOT_TOKEN': '', 'YANDEX_DISK_TOKEN': '', 'YANDEX_MAPS_API_KEY': ''}
    required = set(fixed) | {'R2_DB_PASSWORD', 'ARM2_TABLE_SIGNING_KEY', 'R2_SOURCE_SHA', 'R2_HTTP_PORT', 'APP_ORIGIN'}
    require(set(e) == required, 'env: unknown/missing keys (integrations are forbidden)')
    require(all(e[k] == v for k, v in fixed.items()), 'env: test-only settings/tokens invalid')
    for key in ('R2_DB_PASSWORD', 'ARM2_TABLE_SIGNING_KEY'):
        require(bool(re.fullmatch('[0-9a-f]{64}', e[key])), 'env: generate independent 32-byte hex secrets')
    require(e['R2_DB_PASSWORD'] != e['ARM2_TABLE_SIGNING_KEY'], 'env: secrets must be independent')
    require(bool(re.fullmatch('[0-9a-f]{40}', e['R2_SOURCE_SHA'])), 'env: exact reviewed source SHA required')
    port = e['R2_HTTP_PORT']
    require(port.isascii() and port.isdigit() and 1024 <= int(port) <= 65535 and int(port) != 8080,
            'port: explicit non-B1 unprivileged port required')
    require(e['APP_ORIGIN'] == 'http://127.0.0.1:' + port, 'origin: must match R2 loopback URL')


def mounts(service):
    return {(v['source'], v['target'], v['type']) for v in service.get('volumes', [])}


def validate_config(c, e, root=ROOT):
    require(c.get('name') == PROJECT and set(c['services']) == SERVICES, 'compose: exact project/three services required')
    require(set(c['volumes']) == set(VOLUMES) and set(c['networks']) == set(NETWORKS), 'compose: unknown resources')
    for group, names in (('volumes', VOLUMES), ('networks', NETWORKS)):
        for key, name in names.items():
            resource = c[group][key]
            require(resource['name'] == name and not resource.get('external'), 'compose: foreign/external resource')
            require(not any(resource.get(k) for k in ('driver_opts', 'ipam', 'attachable'))
                    and resource.get('driver', 'local' if group == 'volumes' else 'bridge')
                    == ('local' if group == 'volumes' else 'bridge'), 'compose: custom/shared resource driver forbidden')
    require(c['networks']['r2_internal'].get('internal') is True, 'compose: internal DB/API network required')
    require(not c['networks']['r2_ingress'].get('internal'), 'compose: dedicated web ingress required')
    db, api, web = (c['services'][n] for n in ('r2-db', 'r2-api', 'r2-web'))
    require(db['image'] == 'postgis/postgis:16-3.5', 'compose: unexpected DB image')
    require(db['environment'] == {'POSTGRES_DB': 'arm2_r2_uat', 'POSTGRES_USER': 'arm2r2',
                                 'POSTGRES_PASSWORD': e['R2_DB_PASSWORD']}, 'compose: wrong DB identity')
    require(api.get('user') == '10001:10001', 'compose: API must be unprivileged')
    expected_env = {k: e[k] for k in ('APP_ENV', 'ORGANIZATIONS_ENABLED', 'DATA_DIR', 'COOKIE_SECURE',
                                     'APP_ORIGIN', 'ARM2_TABLE_SIGNING_KEY', 'MAX_BOT_TOKEN',
                                     'YANDEX_DISK_TOKEN', 'YANDEX_MAPS_API_KEY')}
    expected_env['DATABASE_URL'] = 'postgresql+psycopg://arm2r2:' + e['R2_DB_PASSWORD'] + '@r2-db:5432/arm2_r2_uat'
    require(api['environment'] == expected_env, 'compose: unexpected API environment/DSN')
    expected_mounts = ({('r2_postgres_data', '/var/lib/postgresql/data', 'volume')},
                       {('r2_photo_data', '/data', 'volume'), ('r2_file_data', '/files', 'volume')}, set())
    for service, expected in zip((db, api, web), expected_mounts, strict=True):
        require(mounts(service) == expected, 'compose: only exact R2 volumes allowed')
        require(service.get('healthcheck') and service['healthcheck'].get('test'), 'compose: missing healthcheck')
        require(not any(service.get(k) for k in ('privileged', 'network_mode', 'pid', 'container_name',
                                                'devices', 'volumes_from', 'external_links', 'env_file', 'entrypoint', 'command')),
                'compose: unexpected runtime override')
    require(not db.get('ports') and not api.get('ports'), 'compose: DB/API publishing forbidden')
    ports = web.get('ports', [])
    require(len(ports) == 1 and ports[0].get('host_ip') == '127.0.0.1'
            and str(ports[0].get('published')) == e['R2_HTTP_PORT'] and ports[0].get('target') == 80
            and ports[0].get('protocol') == 'tcp', 'compose: loopback web port mismatch')
    require(set(db['networks']) == {'r2_internal'} and set(api['networks']) == {'r2_internal'}
            and set(web['networks']) == set(NETWORKS), 'compose: network separation invalid')
    require(api['networks']['r2_internal'].get('aliases') == ['api'], 'compose: API DNS alias missing')
    for n, service, folder in (('api', api, 'backend'), ('web', web, 'frontend')):
        require(service['image'] == f'arm2-r2-{n}:' + e['R2_SOURCE_SHA'] and service.get('pull_policy') == 'never',
                'compose: only local SHA-tagged R2 builds allowed')
        require(Path(service['build']['context']) == root / folder
                and service['build'].get('dockerfile', 'Dockerfile') == 'Dockerfile', 'compose: build context mismatch')
    require(web['build'].get('target') == 'operators', 'compose: operators stage required')


def validate_state(containers, volumes, networks, phase, port):
    """Summaries only: unrelated containers' environment is never requested."""
    selected = [c for c in containers if c.get('Labels', {}).get('com.docker.compose.project') == PROJECT]
    owned_ids = {c['Id'] for c in selected}
    require(len({c['Labels'].get('com.docker.compose.service') for c in selected}) == len(selected),
            'docker: duplicate R2 service')
    if phase == 'fresh':
        require(not selected, 'fresh: existing R2 containers; STOP, do not adopt/delete')
        require(not any(v['Name'] in VOLUMES.values() for v in volumes), 'fresh: existing R2 volumes; STOP')
        require(not any(n['Name'] in NETWORKS.values() for n in networks), 'fresh: existing R2 networks; STOP')
    else:
        require({c['Labels'].get('com.docker.compose.service') for c in selected} == SERVICES,
                'ready: exactly three R2 containers required')
        for group, names, label in ((volumes, VOLUMES, 'volume'), (networks, NETWORKS, 'network')):
            for key, name in names.items():
                found = [v for v in group if v['Name'] == name]
                require(len(found) == 1 and (found[0].get('Labels') or {}).get('com.docker.compose.project') == PROJECT
                        and (found[0].get('Labels') or {}).get('com.docker.compose.' + label) == key,
                        'ready: missing/foreign R2 resource labels')
                require(not found[0].get('Options') and found[0].get('Driver') == ('local' if label == 'volume' else 'bridge'),
                        'ready: custom/shared resource backing forbidden')
        internal = next(n for n in networks if n['Name'] == NETWORKS['r2_internal'])
        require(internal.get('Internal') is True, 'ready: internal network mismatch')
        for c in selected:
            service = c['Labels']['com.docker.compose.service']
            actual = {(m.get('Name'), m['Destination'], m['Type']) for m in c['Mounts']}
            expected = ({(VOLUMES['r2_postgres_data'], '/var/lib/postgresql/data', 'volume')} if service == 'r2-db' else
                        {(VOLUMES['r2_photo_data'], '/data', 'volume'), (VOLUMES['r2_file_data'], '/files', 'volume')}
                        if service == 'r2-api' else set())
            require(actual == expected, 'ready: container mounts mismatch')
            require(set(c['Networks']) == ({NETWORKS['r2_internal']} if service != 'r2-web' else set(NETWORKS.values())),
                    'ready: container networks mismatch')
            if service != 'r2-web':
                require(not any(c['Ports'].values()), 'ready: DB/API published')
            else:
                require(c['Ports'] == {'80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]},
                        'ready: exact web loopback binding required')
    for c in containers:
        if c['Id'] not in owned_ids:
            require(not any(m.get('Name') in VOLUMES.values() for m in c['Mounts']), 'docker: shared/foreign R2 volume consumer')
            require(not set(c['Networks']) & set(NETWORKS.values()), 'docker: shared R2 network with B1/other container')
        for bindings in c['Ports'].values():
            for binding in bindings or []:
                if int(binding['HostPort']) == port:
                    require(phase == 'ready' and c['Id'] in owned_ids
                            and c['Labels']['com.docker.compose.service'] == 'r2-web'
                            and binding['HostIp'] == '127.0.0.1', 'port: collision with B1/another listener')
    return selected


def validate_revision(value):
    require(value.strip() == '0070', 'database: unknown/not-ready Alembic revision (expected 0070); STOP')


def validate_capacity(ram, checkout_free, docker_free):
    require(ram >= MIN_RAM, 'capacity: at least 2 GiB available RAM required')
    require(checkout_free >= MIN_DISK and docker_free >= MIN_DISK, 'capacity: at least 15 GiB free checkout/Docker disk required')


def capture(args, *, cwd=None, env=None):
    # Never echo stderr/config/DSNs; only the operation category is safe to expose.
    try:
        return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=45).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise NoGo('read-only command failed: ' + args[0] + '; inspect privately, no automatic repair') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--phase', choices=('fresh', 'ready'), required=True)
    args = parser.parse_args()
    try:
        require(os.name == 'posix' and Path('/proc/meminfo').exists(), 'host: Ubuntu/Linux operational preflight required')
        require(socket.gethostname() == 'arm2-ubuntu-uat-001', 'host: exact Ubuntu UAT VM required')
        release = Path('/etc/os-release').read_text()
        require(re.search(r'^ID=ubuntu$', release, re.MULTILINE)
                and re.search(r'^VERSION_ID="22\.04"$', release, re.MULTILINE), 'host: expected Ubuntu 22.04 required')
        require(Path.cwd() == ROOT and Path.cwd().resolve() == ROOT, 'directory: exact non-symlink R2 candidate required')
        e = read_env(args.env_file)
        require(not any(os.environ.get(k) for k in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES')),
                'host: Docker/Compose ambient overrides forbidden')
        require(capture(['git', 'rev-parse', 'HEAD']).strip() == e['R2_SOURCE_SHA'], 'source: checkout SHA mismatch')
        capture(['git', 'merge-base', '--is-ancestor', BASE, 'HEAD'])
        require(not capture(['git', 'status', '--porcelain']).strip(), 'source: checkout must be clean')
        ram = re.search(r'^MemAvailable:\s+(\d+) kB$', Path('/proc/meminfo').read_text(), re.MULTILINE)
        require(ram, 'capacity: unknown available RAM')
        contexts = json.loads(capture(['docker', 'context', 'inspect']))
        require(len(contexts) == 1 and contexts[0]['Endpoints']['docker']['Host'] == 'unix:///var/run/docker.sock',
                'docker: local standard socket required; remote contexts forbidden')
        info = json.loads(capture(['docker', 'info', '--format', '{{json .}}']))
        require(info['OSType'] == 'linux', 'docker: Linux daemon required')
        validate_capacity(int(ram[1]) * 1024, shutil.disk_usage(ROOT).free, shutil.disk_usage(info['DockerRootDir']).free)
        # Environment allowlist prevents ambient shell values overriding the private env-file.
        compose_env = {k: os.environ[k] for k in ('PATH', 'HOME') if k in os.environ}
        config = json.loads(capture(['docker', 'compose', '--env-file', str(args.env_file.resolve()), '-p', PROJECT,
                                    '-f', 'compose.arm2-r2-uat.yaml', 'config', '--format', 'json'], env=compose_env))
        validate_config(config, e)
        ids = capture(['docker', 'ps', '-aq']).split()
        template = ('{"Id":{{json .Id}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}},'
                    '"Networks":{{json .NetworkSettings.Networks}},"Ports":{{json .NetworkSettings.Ports}}}')
        containers = [json.loads(s) for s in capture(['docker', 'inspect', '--format', template, *ids]).splitlines()] if ids else []
        for c in containers:
            c['Labels'] = c['Labels'] or {}
        def resources(kind):
            names = capture(['docker', kind, 'ls', '-q']).split()
            return json.loads(capture(['docker', kind, 'inspect', *names])) if names else []
        volumes, networks = resources('volume'), resources('network')
        selected = validate_state(containers, volumes, networks, args.phase, int(e['R2_HTTP_PORT']))
        own_binding = any(b for c in selected for bs in c['Ports'].values() for b in bs or []
                          if b['HostPort'] == e['R2_HTTP_PORT'])
        if not own_binding:
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', int(e['R2_HTTP_PORT'])))
                except OSError as exc:
                    raise NoGo('port: host listener/collision; select another verified port') from exc
        if args.phase == 'ready':
            details = json.loads(capture(['docker', 'inspect', *[c['Id'] for c in selected]]))
            for c in details:
                service = c['Config']['Labels']['com.docker.compose.service']
                require(c['State'].get('Health', {}).get('Status') == 'healthy', 'ready: unhealthy/not-running R2 service')
                expected = config['services'][service]
                require(c['Config']['Image'] == expected['image'], 'ready: image SHA mismatch')
                if service == 'r2-api':
                    runtime = dict(v.split('=', 1) for v in c['Config']['Env'])
                    require(all(runtime.get(k) == v for k, v in expected['environment'].items())
                            and c['Config']['User'] == '10001:10001', 'ready: API environment/user mismatch')
                    require('api' in c['NetworkSettings']['Networks'][NETWORKS['r2_internal']]['Aliases'], 'ready: DNS alias mismatch')
            dbid = next(c['Id'] for c in selected if c['Labels']['com.docker.compose.service'] == 'r2-db')
            query = ['docker', 'exec', dbid, 'psql', '-XAt', '-U', 'arm2r2', '-d', 'arm2_r2_uat', '-v', 'ON_ERROR_STOP=1', '-c']
            validate_revision(capture([*query, 'SELECT version_num FROM alembic_version']))
            require(capture([*query, 'SELECT current_database()']).strip() == 'arm2_r2_uat', 'ready: database identity mismatch')
        print(json.dumps({'status': 'GO', 'phase': args.phase, 'project': PROJECT, 'directory': str(ROOT),
                          'database': 'arm2_r2_uat', 'volumes': VOLUMES, 'networks': NETWORKS,
                          'loopback_port': int(e['R2_HTTP_PORT']), 'mode': 'read-only; no host changes'}))
    except NoGo as exc:
        print('NO-GO: ' + str(exc))
        raise SystemExit(1) from None
    except Exception:  # noqa: BLE001 -- fail closed without leaking Docker stderr/config/secrets
        print('NO-GO: unknown/incomplete host or Docker state; inspect privately, no automatic repair')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
