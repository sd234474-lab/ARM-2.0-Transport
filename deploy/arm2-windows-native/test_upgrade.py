"""Boundary tests without a connection to any installed database."""
import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import policy
import upgrade

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'arm2-windows-uat'))
import web
from uat_policy import allowed


class UpgradeTests(unittest.TestCase):
    def test_head_search_is_404_with_no_body_on_wire(self):
        # Real HTTP bytes: APIRequestContext correctly rejects a body after HEAD.
        class QuietHandler(web.Handler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for resource in ['types', 'vehicles', 'counterparties', 'organizations']:
                with socket.create_connection(server.server_address, timeout=2) as sock:
                    sock.sendall(f'HEAD /api/arm2/{resource}/search HTTP/1.0\r\nHost: localhost\r\n\r\n'.encode())
                    chunks = []
                    while data := sock.recv(4096):
                        chunks.append(data)
                headers, body = b''.join(chunks).split(b'\r\n\r\n', 1)
                self.assertIn(b'404 Not Found', headers)
                self.assertEqual(body, b'')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_exact_get_search_only(self):
        for resource in ['types', 'vehicles', 'counterparties', 'organizations']:
            path = '/api/arm2/' + resource + '/search'
            self.assertTrue(allowed('GET', path))
            for method in ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']:
                self.assertFalse(allowed(method, path))
            for suffix in ['/', '/extra', '%2f', '?anything', '\x00']:
                self.assertFalse(allowed('GET', path + suffix))
        for path in ['/api/arm2/documents/search', '/api/arm2/representatives/search',
                     '/api/max/webhook', '/api/integrations', '/api/vehicles', '/api/ml/train']:
            for method in ['GET', 'POST', 'PUT', 'DELETE']:
                self.assertFalse(allowed(method, path))

    def test_old_crud_and_history_preserved(self):
        for resource, key in [('types', 'TYPE'), ('vehicles', 'VEH'), ('counterparties', 'CP'), ('organizations', 'ORG')]:
            for method in ['GET', 'POST']:
                self.assertTrue(allowed(method, f'/api/arm2/{resource}'))
            for method in ['GET', 'PUT']:
                self.assertTrue(allowed(method, f'/api/arm2/{resource}/{key}-000001'))
            self.assertTrue(allowed('GET', f'/api/arm2/history/{key}/{key}-000001'))
            self.assertFalse(allowed('POST', f'/api/arm2/history/{key}/{key}-000001'))

    def test_metadata_cannot_adopt_foreign_instance_or_incomplete_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'private').mkdir()
            identity = {'root': str(root), 'approved_head': policy.APPROVED_HEAD,
                        'origin_head': policy.ORIGIN_HEAD, 'source_sha': policy.APP_SOURCE,
                        'package': policy.PACKAGE, 'instance_id': 'a' * 32}
            file = root / 'private/instance.json'
            file.write_text(json.dumps(identity))
            release = {'source_sha': policy.APP_SOURCE, 'instance_id': 'b' * 32,
                       'package_head': 'c' * 40, 'files': {}}
            (root / 'private/release.json').write_text(json.dumps(release))
            with patch.object(policy, 'ROOT', root):
                with self.assertRaises(RuntimeError):
                    policy.config()
                for key in ['root', 'approved_head', 'origin_head', 'source_sha', 'package', 'instance_id']:
                    file.write_text(json.dumps(identity | {key: 'foreign'}))
                    with self.assertRaises(RuntimeError):
                        policy.config()

    def test_gate_fails_before_installed_controller_import(self):
        with patch.object(upgrade, 'ROOT', Path('C:/not-an-installed-uat')), patch.object(upgrade, 'old_controller') as controller:
            with self.assertRaises(RuntimeError):
                upgrade.preflight()
            controller.assert_not_called()

    def test_changed_data_stops_install_before_any_process_signal(self):
        before = {'protected_files': {}, 'private_sha': 'same', 'tables': {'existing': 1}}
        current = before | {'tables': {'existing': 2}}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'preflight.json').write_text(json.dumps(before))
            with patch.object(upgrade, 'prepared_folder', return_value=(folder, {})), \
                    patch.object(upgrade, 'preflight', return_value=current), \
                    patch.object(upgrade, 'controller') as controller:
                with self.assertRaises(RuntimeError):
                    upgrade.install()
                controller.assert_not_called()

    def test_readiness_rejects_foreign_pgdata_even_with_correct_database(self):
        db = Mock()
        db.scalar.side_effect = [policy.DB_NAME, 'C:/ARM/CONTRACTOR_MAX/transport-max/.local/postgres-data', '0061', '3.6.2']
        with patch.object(policy, 'config') as config:
            with self.assertRaises(RuntimeError):
                policy.readiness(db)
            config.assert_not_called()

    def test_manifest_requires_real_hashes_and_confines_files_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'private').mkdir()
            identity = {'root': str(root), 'approved_head': policy.APPROVED_HEAD,
                        'origin_head': policy.ORIGIN_HEAD, 'source_sha': policy.APP_SOURCE,
                        'package': policy.PACKAGE, 'instance_id': 'a' * 32}
            (root / 'private/instance.json').write_text(json.dumps(identity))
            files = {}
            for name in policy.RELEASE_FILES:
                file = root / 'source' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'test')
                files[name] = upgrade.sha(file)
            release = {'source_sha': policy.APP_SOURCE, 'instance_id': 'a' * 32,
                       'package_head': 'c' * 40, 'files': files}
            manifest = root / 'private/release.json'
            manifest.write_text(json.dumps(release))
            with patch.object(policy, 'ROOT', root):
                self.assertEqual(policy.config()['package_head'], 'c' * 40)
                file.write_bytes(b'changed')
                with self.assertRaises(RuntimeError):
                    policy.config()
                file.write_bytes(b'test')
                files['../private/instance.json'] = upgrade.sha(root / 'private/instance.json')
                manifest.write_text(json.dumps(release))
                with self.assertRaises(RuntimeError):
                    policy.config()


if __name__ == '__main__':
    unittest.main()
