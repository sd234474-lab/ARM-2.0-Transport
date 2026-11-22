"""Explicit Disk overlay gate; normalize only verified additions into the unchanged base gate."""
import copy
import json
import stat
from pathlib import Path

import preflight as base
from disk_proxy import allowed_hosts

GATEWAY = 'r2-disk-egress'
NETWORK = 'r2_disk_egress'
NETWORK_NAME = base.PROJECT + '_' + NETWORK
PRIVATE = Path('/home/arm2admin/.config/arm2-r2-disk')
EXTRA_ENV = {'ARM2_R2_DISK_CONFIG': '/run/r2-disk/config.json',
             'ARM2_R2_DISK_TOKEN_FILE': '/run/r2-disk/token',
             'HTTPS_PROXY': 'http://r2-disk-egress:3128', 'HTTP_PROXY': 'http://r2-disk-egress:3128',
             'NO_PROXY': '127.0.0.1,localhost,r2-db'}
COMMAND = ['uvicorn', 'app.api:app', '--host', '0.0.0.0', '--port', '8000',
           '--proxy-headers', '--forwarded-allow-ips', '*']


def binds(root, api):
    values = {((root / 'deploy/arm2-r2-uat' / ('disk_launch.py' if api else 'disk_proxy.py')).as_posix(),
               '/opt/r2-disk/' + ('launch.py' if api else 'proxy.py')),
              ((PRIVATE / 'config.json').as_posix(), '/run/r2-disk/config.json')}
    if api:
        values.add(((PRIVATE / 'token').as_posix(), '/run/r2-disk/token'))
    return values


def validate_config(config, env, root=base.ROOT):
    require = base.require
    c = copy.deepcopy(config)
    require(set(c['services']) == base.SERVICES | {GATEWAY}, 'disk: exact four R2 services required')
    require(set(c['networks']) == set(base.NETWORKS) | {NETWORK}, 'disk: unexpected network')
    network = c['networks'].pop(NETWORK)
    require(network.get('name') == NETWORK_NAME and not any(network.get(k) for k in
        ('internal', 'external', 'driver_opts', 'ipam', 'attachable'))
        and network.get('driver', 'bridge') == 'bridge', 'disk: dedicated egress bridge required')
    proxy = c['services'].pop(GATEWAY)
    require(set(proxy) <= {'image', 'pull_policy', 'user', 'restart', 'entrypoint', 'read_only',
        'cap_drop', 'security_opt', 'volumes', 'networks', 'healthcheck'}, 'disk: unknown gateway override')
    require(proxy['image'] == 'arm2-r2-api:' + env['R2_SOURCE_SHA'] and proxy.get('pull_policy') == 'never'
        and proxy.get('user') == '10001:10001' and proxy.get('restart') == 'no'
        and proxy.get('entrypoint') == ['python', '/opt/r2-disk/proxy.py']
        and proxy.get('read_only') is True and proxy.get('cap_drop') == ['ALL']
        and proxy.get('security_opt') == ['no-new-privileges:true'], 'disk: gateway confinement mismatch')
    require(set(proxy['networks']) == {'r2_internal', NETWORK}, 'disk: only gateway may have egress')
    require(proxy.get('healthcheck', {}).get('test') == ['CMD', 'python', '-c',
        "import socket; socket.create_connection(('127.0.0.1',3128),2).close()"], 'disk: gateway healthcheck mismatch')
    api = c['services']['r2-api']
    for service, is_api in ((proxy, False), (api, True)):
        actual = [v for v in service.get('volumes', []) if v['type'] == 'bind']
        require({(v['source'], v['target']) for v in actual} == binds(root, is_api)
            and len(actual) == len(binds(root, is_api))
            and all(v.get('read_only') is True and v.get('bind', {}).get('create_host_path') is False for v in actual),
            'disk: exact read-only script/config/secret bindings required')
        if not is_api:
            require(len(service['volumes']) == len(actual), 'disk: gateway cannot mount DB/runtime/token volumes')
    for key, expected in EXTRA_ENV.items():
        require(api['environment'].pop(key, None) == expected, 'disk: proxy/token-file environment mismatch')
    require(api.pop('entrypoint', None) == ['python', '/opt/r2-disk/launch.py']
        and api.pop('command', None) == COMMAND, 'disk: exact R2 token loader required')
    dependency = api['depends_on'].pop(GATEWAY, None)
    require(dependency and dependency.get('condition') == 'service_healthy', 'disk: healthy gateway dependency required')
    api['volumes'] = [v for v in api['volumes'] if v['type'] != 'bind']
    base.validate_config(c, env, root)


def validate_files():
    token = PRIVATE / 'token'
    meta = token.lstat()
    base.require(stat.S_ISREG(meta.st_mode) and stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_uid == 10001,
                 'disk: service-owned 0600 regular token required')
    base.require(PRIVATE.is_dir() and not PRIVATE.is_symlink() and stat.S_IMODE(PRIVATE.stat().st_mode) == 0o700,
                 'disk: private 0700 configuration directory required')
    config = PRIVATE / 'config.json'
    base.require(config.is_file() and not config.is_symlink() and not (config.stat().st_mode & 0o022),
                 'disk: protected non-writable public configuration required')
    value = json.loads(config.read_text())
    allowed_hosts(value)
    base.require(set(value) == {'account_uid', 'resource_id', 'document_root', 'allowed_hosts'}
        and value['account_uid'].isdigit() and value['resource_id'].startswith(value['account_uid'] + ':')
        and value['document_root'].startswith('disk:/'), 'disk: verified root identity required')


def validate_state(containers, volumes, networks, phase, port):
    require = base.require
    require(phase == 'ready', 'disk: adopt only the existing R2, never a fresh installation')
    proxy = [c for c in containers if c['Labels'].get('com.docker.compose.project') == base.PROJECT
             and c['Labels'].get('com.docker.compose.service') == GATEWAY]
    require(len(proxy) == 1, 'disk: exactly one owned gateway required')
    gateway = proxy[0]
    require(set(gateway['Networks']) == {base.NETWORKS['r2_internal'], NETWORK_NAME}
        and not any(gateway['Ports'].values()), 'disk: gateway networks/ports mismatch')
    require({(m['Source'], m['Destination']) for m in gateway['Mounts']} == binds(base.ROOT, False)
        and all(m['Type'] == 'bind' and not m['RW'] for m in gateway['Mounts']), 'disk: gateway mounts mismatch')
    own_networks = [n for n in networks if n['Name'] == NETWORK_NAME]
    require(len(own_networks) == 1, 'disk: missing egress network')
    n = own_networks[0]
    require(not n['Internal'] and n['Driver'] == 'bridge' and not n.get('Options')
        and n['Labels'].get('com.docker.compose.project') == base.PROJECT
        and n['Labels'].get('com.docker.compose.network') == NETWORK, 'disk: foreign/custom egress network')
    normalized = []
    for c in containers:
        if c['Id'] == gateway['Id']:
            continue
        require(NETWORK_NAME not in c['Networks'], 'disk: API/DB/web/foreign container has egress')
        c = copy.deepcopy(c)
        if c['Labels'].get('com.docker.compose.project') == base.PROJECT and c['Labels'].get('com.docker.compose.service') == 'r2-api':
            extra = [m for m in c['Mounts'] if m['Type'] == 'bind']
            require({(m['Source'], m['Destination']) for m in extra} == binds(base.ROOT, True)
                and all(not m['RW'] for m in extra), 'disk: API live bindings mismatch')
            c['Mounts'] = [m for m in c['Mounts'] if m['Type'] != 'bind']
        normalized.append(c)
    selected = base.validate_state(normalized, volumes, [n for n in networks if n['Name'] != NETWORK_NAME], phase, port)
    return selected + proxy


def validate_runtime(details, config):
    for item in details:
        service = item['Config']['Labels']['com.docker.compose.service']
        if service not in {'r2-api', GATEWAY}:
            continue
        expected = config['services'][service]
        base.require(item['Config']['User'] == '10001:10001'
            and item['Config']['Entrypoint'] == expected['entrypoint'], 'disk: live user/entrypoint mismatch')
        if service == 'r2-api':
            base.require(item['Config']['Cmd'] == COMMAND, 'disk: live API command mismatch')
        else:
            host = item['HostConfig']
            base.require(host['ReadonlyRootfs'] and host['CapDrop'] == ['ALL']
                and host['SecurityOpt'] == ['no-new-privileges:true'], 'disk: live gateway confinement mismatch')
