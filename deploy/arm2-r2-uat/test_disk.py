"""Synthetic, bounded tests. No live token, cloud account, database or VM."""
import copy
import importlib.util
import json
import re
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import disk_launch as launch
import disk_preflight as disk
import disk_proxy as proxy
import preflight as base

spec = importlib.util.spec_from_file_location('base_test_fixtures', HERE / 'test_preflight.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def config():
    c = fixtures.config()
    raw = (HERE.parents[1] / 'compose.arm2-r2-disk.yaml').read_text()
    overlay = json.loads(re.sub(r'\$\{([A-Z0-9_]+):\?[^}]+}', lambda m: fixtures.env()[m[1]], raw))
    c['networks'].update(overlay['networks'])
    for name, extra in overlay['services'].items():
        if name not in c['services']:
            c['services'][name] = copy.deepcopy(extra)
        else:
            for key, value in extra.items():
                if key in {'environment', 'depends_on'}:
                    c['services'][name].setdefault(key, {}).update(value)
                elif key == 'volumes':
                    c['services'][name][key].extend(copy.deepcopy(value))
                else:
                    c['services'][name][key] = value
    for service in c['services'].values():
        if isinstance(service['networks'], list):
            service['networks'] = {n: None for n in service['networks']}
        for volume in service.get('volumes', []):
            if volume['type'] == 'bind' and volume['source'].startswith('./'):
                volume['source'] = (base.ROOT / volume['source'][2:]).as_posix()
    return c


class DiskTests(unittest.TestCase):
    def test_token_is_read_only_from_a_private_regular_service_owned_file(self):
        source = unittest.mock.Mock()
        source.read_text.return_value = 'synthetic-test-token-not-a-live-credential'
        with patch.object(launch.os, 'geteuid', return_value=10001, create=True):
            source.lstat.return_value = SimpleNamespace(st_mode=0o100600, st_uid=10001)
            self.assertEqual(launch.private_token(source), source.read_text.return_value)
            for mode, uid in [(0o100644, 10001), (0o100600, 0), (0o120600, 10001)]:
                source.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
                with self.assertRaises(ValueError):
                    launch.private_token(source)

    def test_overlay_preserves_exact_base_guards(self):
        disk.validate_config(config(), fixtures.env())
        with self.assertRaises(base.NoGo):
            base.validate_config(config(), fixtures.env())  # Disk requires explicit mode.

    def test_overlay_rejects_egress_or_secrets_outside_contract(self):
        for mutate in (
            lambda c: c['services']['r2-api']['networks'].update(r2_disk_egress=None),
            lambda c: c['services']['r2-db']['networks'].update(r2_disk_egress=None),
            lambda c: c['services']['r2-web'].update(ports=[{'target': 80, 'published': '18093', 'host_ip': '0.0.0.0'}]),
            lambda c: c['services']['r2-api']['environment'].update(YANDEX_DISK_TOKEN='synthetic-forbidden'),
            lambda c: c['services']['r2-api']['environment'].update(HTTPS_PROXY='http://other:3128'),
            lambda c: c['services']['r2-api'].update(user='0:0'),
            lambda c: c['services']['r2-disk-egress'].update(ports=[{'published': '3128'}]),
            lambda c: c['services']['r2-disk-egress'].update(read_only=False),
            lambda c: c['services']['r2-disk-egress'].update(privileged=True),
            lambda c: c['services']['r2-disk-egress']['volumes'].append({'source': 'r2_file_data', 'target': '/files', 'type': 'volume'}),
            lambda c: c['services']['r2-api']['volumes'][-1].update(read_only=False),
            lambda c: c['networks']['r2_internal'].update(internal=False),
        ):
            c = config()
            mutate(c)
            with self.subTest(mutate=mutate), self.assertRaises(base.NoGo):
                disk.validate_config(c, fixtures.env())

    def test_gateway_policy_rejects_non_disk_and_private_addresses(self):
        hosts = proxy.allowed_hosts({'allowed_hosts': ['cloud-api.yandex.net', 'uploader10o.disk.yandex.net']})
        for authority in ['example.org:443', 'cloud-api.yandex.net:80', '127.0.0.1:443',
                          'cloud-api.yandex.net.evil.invalid:443', 'user@cloud-api.yandex.net:443']:
            with self.subTest(authority=authority), self.assertRaises(ValueError):
                proxy.destination(authority, hosts)
        for ip in ['127.0.0.1', '172.20.0.2', '169.254.169.254', '::1']:
            with patch.object(proxy.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]), self.assertRaises(ValueError):
                proxy.destination('cloud-api.yandex.net:443', hosts)
        with patch.object(proxy.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('77.88.55.88', 443))]):
            self.assertEqual(proxy.destination('cloud-api.yandex.net:443', hosts)[3], ('77.88.55.88', 443))
        for host in ['yandex.ru', 'maps.yandex.ru', 'evil.yandex.net', '*.yandex.net', 'example.org']:
            with self.assertRaises(ValueError):
                proxy.allowed_hosts({'allowed_hosts': ['cloud-api.yandex.net', host]})

    def test_real_proxy_denies_http_and_unapproved_connect_without_network_attempt(self):
        with proxy.Server(('127.0.0.1', 0), proxy.Tunnel) as server:
            server.allowlist = {'cloud-api.yandex.net'}
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for request in [b'GET http://example.org/ HTTP/1.1\r\n\r\n',
                                b'CONNECT example.org:443 HTTP/1.1\r\n\r\n',
                                b'CONNECT cloud-api.yandex.net:80 HTTP/1.1\r\n\r\n']:
                    with socket.create_connection(server.server_address, timeout=3) as client:
                        client.sendall(request)
                        self.assertIn(b'403 Forbidden', client.recv(1024))
            finally:
                server.shutdown()
                worker.join(3)

    def test_live_state_rejects_foreign_consumers_and_api_direct_egress(self):
        containers, volumes, networks = fixtures.state()
        for c in containers:
            if c['Id'] == 'r2-api':
                c['Mounts'].extend({'Source': src, 'Destination': dst, 'Type': 'bind', 'RW': False}
                                   for src, dst in disk.binds(base.ROOT, True))
        gateway = {'Id': 'proxy', 'Labels': {'com.docker.compose.project': base.PROJECT,
            'com.docker.compose.service': disk.GATEWAY}, 'Mounts': [
                {'Source': s, 'Destination': t, 'Type': 'bind', 'RW': False} for s, t in disk.binds(base.ROOT, False)],
            'Networks': {base.NETWORKS['r2_internal']: {}, disk.NETWORK_NAME: {}}, 'Ports': {}}
        containers.append(gateway)
        networks.append({'Name': disk.NETWORK_NAME, 'Driver': 'bridge', 'Options': {}, 'Internal': False,
            'Labels': {'com.docker.compose.project': base.PROJECT, 'com.docker.compose.network': disk.NETWORK}})
        self.assertEqual(len(disk.validate_state(containers, volumes, networks, 'ready', 18093)), 4)
        for target in [0, 1, 2]:
            bad = copy.deepcopy(containers)
            bad[target]['Networks'][disk.NETWORK_NAME] = {}
            with self.assertRaises(base.NoGo):
                disk.validate_state(bad, volumes, networks, 'ready', 18093)
        foreign = fixtures.b1()
        foreign['Networks'][disk.NETWORK_NAME] = {}
        with self.assertRaises(base.NoGo):
            disk.validate_state(containers + [foreign], volumes, networks, 'ready', 18093)


if __name__ == '__main__':
    unittest.main()
