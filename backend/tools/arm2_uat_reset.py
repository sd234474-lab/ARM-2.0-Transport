"""Host-side R2 reset. Plan by default; never run automatically or from migrations.

python3 backend/tools/arm2_uat_reset.py --project arm2-ubuntu-uat-r2
Add --execute --confirm-project arm2-ubuntu-uat-r2 only after a separate UAT reset authorization.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def docker(*args, input_bytes=None):
    result = subprocess.run(['docker', *args], input=input_bytes, capture_output=True, check=True)
    return result.stdout


def inspect_instance(project):
    if not re.fullmatch(r'arm2-ubuntu-uat-r2(?:-[a-z0-9]+)?', project):
        raise ValueError('Only a specifically selected R2 project is allowed')
    ids = docker('ps', '-aq').decode().split()
    # Inspect only mounts/labels for unrelated containers, never their environment/credentials.
    template = '{"Id":{{json .Id}},"Mounts":{{json .Mounts}},"Labels":{{json .Config.Labels}}}'
    containers = [json.loads(line) for line in docker('inspect', '--format', template, *ids).splitlines()] if ids else []
    targets = [c for c in containers if (c['Labels'] or {}).get('com.docker.compose.project') == project]
    if len(targets) != 3:
        raise ValueError('Expected exactly three selected R2 service containers')
    selected = {c['Config']['Labels'].get('com.docker.compose.service'): c
                for c in json.loads(docker('inspect', *[c['Id'] for c in targets]))}
    if set(selected) != {'r2-db', 'r2-api', 'r2-web'}:
        raise ValueError('Expected exactly the selected R2 DB/API/web; no parallel reset/tools container')
    api, db, web = (selected[n] for n in ('r2-api', 'r2-db', 'r2-web'))
    env = dict(v.split('=', 1) for v in api['Config']['Env'])
    if env.get('APP_ENV') != 'test' or env.get('MAX_BOT_TOKEN') or env.get('YANDEX_DISK_TOKEN'):
        raise ValueError('Not a disconnected test-only R2 API')
    mounts = {m['Destination']: m for m in api['Mounts']}
    expected = {'/files': project + '_r2_file_data', '/data': project + '_r2_photo_data'}
    for destination, name in expected.items():
        if mounts.get(destination, {}).get('Type') != 'volume' or mounts[destination].get('Name') != name:
            raise ValueError('FILE/runtime mounts do not belong to selected R2')
    pg = [m for m in db['Mounts'] if m['Destination'] == '/var/lib/postgresql/data']
    if len(pg) != 1 or pg[0].get('Name') != project + '_r2_postgres_data':
        raise ValueError('PostgreSQL volume is not the selected R2 volume')
    if any(db['NetworkSettings']['Ports'].values()):
        raise ValueError('Reset refuses externally published PostgreSQL')
    volumes = {*expected.values(), pg[0]['Name']}
    for c in containers:
        if c['Id'] not in {api['Id'], db['Id']} and any(m.get('Name') in volumes for m in c['Mounts']):
            raise ValueError('Selected volumes have another consumer; reset refused')
    networks = api['NetworkSettings']['Networks']
    if len(networks) != 1 or set(networks) != set(db['NetworkSettings']['Networks']):
        raise ValueError('DB/API network isolation is not verified')
    network = json.loads(docker('network', 'inspect', next(iter(networks))))[0]
    if not network['Internal'] or any(c['Name'] not in {api['Name'].lstrip('/'), db['Name'].lstrip('/'), web['Name'].lstrip('/')}
                                     for c in network['Containers'].values()):
        raise ValueError('R2 internal network is shared')
    return selected, env, next(iter(networks)), expected


def run(project, execute=False, confirmation=None):
    if execute and confirmation != project:
        raise ValueError('Explicit --confirm-project must match the selected R2')
    selected, env, network, mounts = inspect_instance(project)
    attestation = {'project': project, 'file_volume': mounts['/files'], 'data_volume': mounts['/data'], 'offline': False}
    descriptor, filename = tempfile.mkstemp(prefix='arm2-r2-reset-', suffix='.env')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            for key, value in env.items():
                if '\n' in value or '\r' in value:
                    raise ValueError('Multiline environment is not supported')
                f.write(key + '=' + value + '\n')
        Path(filename).chmod(0o600)
        base = ['run', '--rm', '-i', '--network', network, '--env-file', filename,
                '--label', 'com.docker.compose.project=' + project,
                '--label', 'com.docker.compose.service=r2-reset',
                '--name', project + '-reset-' + uuid4().hex,
                '-v', mounts['/files'] + ':/files', '-v', mounts['/data'] + ':/data',
                '--entrypoint', 'python']

        def command(write):
            attestation['offline'] = write
            return docker(*base, '-e', 'ARM2_UAT_RESET_ATTESTATION=' + json.dumps(attestation),
                          selected['r2-api']['Image'], '-',
                          '--file-root', '/files', '--data-root', '/data', *(['--execute'] if write else []),
                          input_bytes=(Path(__file__).resolve().parents[1] / 'app/arm2/uat_reset.py').read_bytes())

        plan = command(False)  # Validate exact DB and roots before stopping any service.
        if not execute:
            return json.loads(plan)
        docker('stop', selected['r2-web']['Id'], selected['r2-api']['Id'])
        verified, _, verified_network, verified_mounts = inspect_instance(project)
        if (any(verified[k]['Id'] != selected[k]['Id'] for k in selected)
                or verified_network != network or verified_mounts != mounts):
            raise ValueError('R2 identities changed while taking HTTP offline')
        # A failed/unknown cleanup deliberately leaves only this R2 HTTP pair offline.
        # Retry the same command after recovery; B1 is never stopped or inspected for credentials.
        result = json.loads(command(True))
        docker('start', selected['r2-api']['Id'], selected['r2-web']['Id'])
        return result
    finally:
        Path(filename).unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True)
    p.add_argument('--confirm-project')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    try:
        result = run(args.project, args.execute, args.confirm_project)
    except Exception:
        raise SystemExit('R2 reset refused/interrupted; credentials suppressed. Check the selected offline instance and retry the same project. No B1/volume/container deletion.') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
