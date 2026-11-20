"""Docker boundary unit checks; actual SQL/FILE reset is exercised in Issue72 smoke."""
import json

import pytest

from tools import arm2_uat_reset as host

PROJECT = 'arm2-ubuntu-uat-r2'


def docker_fixture(monkeypatch, *, shared=False, fail_execute=False):
    calls = []
    containers = []
    for service in ('api', 'db', 'web'):
        mounts = ([{'Destination': '/files', 'Name': PROJECT + '_r2_file_data', 'Type': 'volume'},
                   {'Destination': '/data', 'Name': PROJECT + '_r2_photo_data', 'Type': 'volume'}] if service == 'api' else
                  [{'Destination': '/var/lib/postgresql/data', 'Name': PROJECT + '_r2_postgres_data', 'Type': 'volume'}] if service == 'db' else [])
        containers.append({'Id': service, 'Name': '/' + PROJECT + '-r2-' + service + '-1', 'Image': 'synthetic-image',
            'Config': {'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.service': 'r2-' + service},
                       'Env': ['APP_ENV=test', 'MAX_BOT_TOKEN=', 'YANDEX_DISK_TOKEN=']},
            'Mounts': mounts, 'NetworkSettings': {'Networks': {PROJECT + '_r2': {}}, 'Ports': {'5432/tcp': None}}})

    def fake(*args, input_bytes=None):
        calls.append(args)
        if args[0] == 'ps':
            return b'api\ndb\nweb\nother\n'
        if args[0] == 'inspect' and '--format' in args:
            items = [{'Id': c['Id'], 'Labels': c['Config']['Labels'], 'Mounts': c['Mounts']} for c in containers]
            items.append({'Id': 'other', 'Labels': {'com.docker.compose.project': 'B1'},
                          'Mounts': [{'Name': PROJECT + '_r2_file_data'}] if shared else []})
            return '\n'.join(json.dumps(c) for c in items).encode()
        if args[0] == 'inspect':
            assert 'other' not in args  # Unrelated environments/credentials are never fetched.
            return json.dumps(containers).encode()
        if args[0] == 'network':
            return json.dumps([{'Internal': True, 'Containers': {c['Id']: {'Name': c['Name'][1:]} for c in containers}}]).encode()
        if args[0] == 'run':
            assert input_bytes and b'def reset(' in input_bytes
            assert '--env-file' in args and '--entrypoint' in args and '--rm' in args
            if '--execute' in args and fail_execute:
                raise RuntimeError('Synthetic unknown reset outcome')
            return json.dumps({'dry_run': '--execute' not in args, 'result': 'PASS', 'counts': {}}).encode()
        if args[0] in ('stop', 'start'):
            assert args[1:] == ('web', 'api') if args[0] == 'stop' else args[1:] == ('api', 'web')
            return b''
        raise AssertionError(args)

    monkeypatch.setattr(host, 'docker', fake)
    return calls


def test_wrong_project_confirmation_or_shared_file_refuses_before_mutation(monkeypatch):
    calls = docker_fixture(monkeypatch, shared=True)
    with pytest.raises(ValueError, match='R2'):
        host.run('arm2-ubuntu-uat', True, 'arm2-ubuntu-uat')
    assert not calls
    with pytest.raises(ValueError, match='confirm'):
        host.run(PROJECT, True, 'other')
    assert not calls
    with pytest.raises(ValueError, match='another consumer'):
        host.run(PROJECT, True, PROJECT)
    assert not any(c[0] in ('run', 'stop', 'start') for c in calls)


def test_plan_never_stops_and_execution_keeps_failed_r2_http_offline(monkeypatch):
    calls = docker_fixture(monkeypatch)
    assert host.run(PROJECT)['dry_run']
    assert not any(c[0] in ('stop', 'start') for c in calls)
    host.run(PROJECT, True, PROJECT)
    assert [c for c in calls if c[0] == 'stop'] == [('stop', 'web', 'api')]
    assert [c for c in calls if c[0] == 'start'] == [('start', 'api', 'web')]
    calls = docker_fixture(monkeypatch, fail_execute=True)
    with pytest.raises(RuntimeError, match='unknown reset outcome'):
        host.run(PROJECT, True, PROJECT)
    assert any(c[0] == 'stop' for c in calls) and not any(c[0] == 'start' for c in calls)
