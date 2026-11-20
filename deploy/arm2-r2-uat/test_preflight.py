"""Focused configuration/gate tests. No Docker, database, socket or VM access."""
import copy
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
spec = importlib.util.spec_from_file_location('r2_preflight', HERE / 'preflight.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def env():
    # Synthetic values, never usable deployment credentials.
    return {'R2_DB_PASSWORD': 'a' * 64, 'ARM2_TABLE_SIGNING_KEY': 'b' * 64,
            'R2_SOURCE_SHA': p.BASE, 'R2_HTTP_PORT': '18093', 'APP_ORIGIN': 'http://127.0.0.1:18093',
            'APP_ENV': 'test', 'ORGANIZATIONS_ENABLED': 'false', 'DATA_DIR': '/data', 'COOKIE_SECURE': 'false',
            'MAX_BOT_TOKEN': '', 'YANDEX_DISK_TOKEN': '', 'YANDEX_MAPS_API_KEY': ''}


def config():
    # JSON is YAML 1.2 notation. This does NOT substitute for the real Compose config command.
    raw = (REPO / 'compose.arm2-r2-uat.yaml').read_text(encoding='utf-8')
    e = env()
    raw = re.sub(r'\$\{([A-Z0-9_]+):\?[^}]+}', lambda m: e[m[1]], raw)
    c = json.loads(raw)
    for s in c['services'].values():
        s['volumes'] = [dict(zip(('source', 'target'), v.split(':')), type='volume') for v in s.get('volumes', [])]
        if isinstance(s['networks'], list):
            s['networks'] = {n: None for n in s['networks']}
        if 'build' in s:
            s['build']['context'] = str(p.ROOT / s['build']['context'].removeprefix('./'))
    return c


def state():
    containers = []
    for service in ('r2-db', 'r2-api', 'r2-web'):
        mounts = ([{'Name': p.VOLUMES['r2_postgres_data'], 'Destination': '/var/lib/postgresql/data', 'Type': 'volume'}]
                  if service == 'r2-db' else
                  [{'Name': p.VOLUMES['r2_file_data'], 'Destination': '/files', 'Type': 'volume'},
                   {'Name': p.VOLUMES['r2_photo_data'], 'Destination': '/data', 'Type': 'volume'}]
                  if service == 'r2-api' else [])
        containers.append({'Id': service, 'Labels': {'com.docker.compose.project': p.PROJECT,
                                                    'com.docker.compose.service': service},
                           'Mounts': mounts, 'Networks': {n: {} for n in (p.NETWORKS.values() if service == 'r2-web'
                                                                      else [p.NETWORKS['r2_internal']])},
                           'Ports': {'80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18093'}]} if service == 'r2-web' else {}})
    volumes = [{'Name': name, 'Driver': 'local', 'Options': {}, 'Labels': {'com.docker.compose.project': p.PROJECT,
                'com.docker.compose.volume': key}} for key, name in p.VOLUMES.items()]
    networks = [{'Name': name, 'Driver': 'bridge', 'Options': {}, 'Internal': key == 'r2_internal',
                 'Labels': {'com.docker.compose.project': p.PROJECT, 'com.docker.compose.network': key}}
                for key, name in p.NETWORKS.items()]
    return containers, volumes, networks


def b1():
    return {'Id': 'b1-db', 'Labels': {'com.docker.compose.project': 'arm2-ubuntu-uat',
                                    'com.docker.compose.service': 'db'},
            'Mounts': [{'Name': 'arm2-ubuntu-uat_postgres_data', 'Destination': '/var/lib/postgresql/data', 'Type': 'volume'}],
            'Networks': {'arm2-ubuntu-uat_default': {}}, 'Ports': {'80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8080'}]}}


class PreflightTests(unittest.TestCase):
    def test_exact_compose_contract_and_nginx_route(self):
        p.validate_config(config(), env())
        self.assertEqual(set(config()['services']), {'r2-db', 'r2-api', 'r2-web'})
        self.assertIn('proxy_pass http://api:8000', (REPO / 'frontend/nginx.conf').read_text())
        self.assertEqual(config()['services']['r2-api']['networks'], {'r2_internal': {'aliases': ['api']}})

    def test_env_fail_closed(self):
        mutations = [{'APP_ENV': 'production'}, {'YANDEX_DISK_TOKEN': 'accidental-test-token'},
                     {'MAX_BOT_TOKEN': 'token'}, {'ARM2_TABLE_SIGNING_KEY': ''}, {'R2_HTTP_PORT': 'UNKNOWN'},
                     {'R2_HTTP_PORT': '8080'}, {'APP_ORIGIN': 'https://working.example'},
                     {'DATABASE_URL': 'postgresql://db/maxphotos'}, {'ORGANIZATIONS_ENABLED': 'true'},
                     {'ARM2_TABLE_SIGNING_KEY': 'a' * 64}]
        for mutation in mutations:
            with self.subTest(mutation=list(mutation)), self.assertRaises(p.NoGo):
                p.validate_env({**env(), **mutation})

    def test_config_collisions_and_overrides(self):
        for mutate in (
            lambda c: c.update(name='arm2-ubuntu-uat'),
            lambda c: c['services'].update(worker={}),
            lambda c: c['volumes']['r2_file_data'].update(name='arm2-ubuntu-uat_photo_data'),
            lambda c: c['volumes']['r2_file_data'].update(driver_opts={'device': '/b1'}),
            lambda c: c['services']['r2-db'].update(ports=[{'published': '5432'}]),
            lambda c: c['networks']['r2_internal'].update(internal=False),
            lambda c: c['services']['r2-api']['networks']['r2_internal'].update(aliases=[]),
            lambda c: c['services']['r2-api'].update(user='0:0'),
            lambda c: c['services']['r2-web']['build'].update(target='citizens'),
        ):
            c = config()
            mutate(c)
            with self.subTest(config=c.get('name')), self.assertRaises(p.NoGo):
                p.validate_config(c, env())

    def test_b1_present_fresh_repeat_is_read_only(self):
        containers = [b1()]
        before = copy.deepcopy(containers)
        for _ in range(2):
            self.assertEqual(p.validate_state(containers, [], [], 'fresh', 18093), [])
        self.assertEqual(containers, before)

    def test_ready_repeat_and_foreign_consumers(self):
        containers, volumes, networks = state()
        containers.append(b1())
        before = copy.deepcopy((containers, volumes, networks))
        for _ in range(2):
            self.assertEqual(len(p.validate_state(containers, volumes, networks, 'ready', 18093)), 3)
        self.assertEqual((containers, volumes, networks), before)
        containers[-1]['Mounts'][0]['Name'] = p.VOLUMES['r2_file_data']
        with self.assertRaisesRegex(p.NoGo, 'volume'):
            p.validate_state(containers, volumes, networks, 'ready', 18093)

    def test_existing_r2_is_not_adopted_for_fresh_install(self):
        c, v, n = state()
        for values in ((c, v, n), ([], v, []), ([], [], n)):
            with self.assertRaises(p.NoGo):
                p.validate_state(*values, 'fresh', 18093)

    def test_port_and_b1_network_collision(self):
        c, v, n = state()
        other = b1()
        other['Ports']['80/tcp'][0]['HostPort'] = '18093'
        with self.assertRaisesRegex(p.NoGo, 'port'):
            p.validate_state([other], [], [], 'fresh', 18093)
        other = b1()
        other['Networks'] = {p.NETWORKS['r2_internal']: {}}
        with self.assertRaisesRegex(p.NoGo, 'network'):
            p.validate_state([*c, other], v, n, 'ready', 18093)

    def test_foreign_volume_and_unknown_alembic(self):
        c, v, n = state()
        v[0]['Labels']['com.docker.compose.project'] = 'B1'
        with self.assertRaises(p.NoGo):
            p.validate_state(c, v, n, 'ready', 18093)
        for revision in ('0061', '0069', 'unknown', '', '0070\n0069'):
            with self.subTest(revision=revision), self.assertRaises(p.NoGo):
                p.validate_revision(revision)
        p.validate_revision('0070\n')

    def test_capacity_fail_closed(self):
        p.validate_capacity(p.MIN_RAM, p.MIN_DISK, p.MIN_DISK)
        for ram, checkout, docker in ((p.MIN_RAM - 1, p.MIN_DISK, p.MIN_DISK),
                                     (p.MIN_RAM, p.MIN_DISK - 1, p.MIN_DISK),
                                     (p.MIN_RAM, p.MIN_DISK, p.MIN_DISK - 1)):
            with self.assertRaisesRegex(p.NoGo, 'capacity'):
                p.validate_capacity(ram, checkout, docker)

    def test_private_env_file_and_output_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'r2.env'
            path.write_text('\n'.join(k + '=' + v for k, v in env().items()))
            path.chmod(0o600)
            # Windows cannot represent POSIX 0600: test the stat boundary explicitly.
            with patch.object(p.stat, 'S_IMODE', return_value=0o600):
                self.assertEqual(p.read_env(path), env())
            with patch.object(p.stat, 'S_IMODE', return_value=0o644), self.assertRaises(p.NoGo):
                p.read_env(path)
        with patch.object(p.subprocess, 'run', side_effect=p.subprocess.CalledProcessError(1, ['docker'], stderr='PRIVATE')):
            with self.assertRaises(p.NoGo) as caught:
                p.capture(['docker', 'compose'])
            self.assertNotIn('PRIVATE', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
