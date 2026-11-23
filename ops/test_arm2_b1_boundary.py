"""No DB access: prove unsafe targets are rejected before a connection is attempted."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm2_b1_check as sandbox


class BoundaryTests(unittest.TestCase):
    def test_protected_or_foreign_dsns_fail_before_connect(self):
        for dsn in [
            'postgresql+psycopg://unused:unused@127.0.0.1:55432/contractor_max_test',
            'postgresql+psycopg://unused:unused@127.0.0.1:55442/arm2_win_run_001',
            'postgresql+psycopg://unused:unused@remote.invalid:55447/arm2_b1_test',
            'postgresql+psycopg://unused:unused@127.0.0.1:55447/other_test',
        ]:
            with self.subTest(dsn=dsn), patch.dict(os.environ, {'DATABASE_URL': dsn}), patch('socket.socket.connect') as connect:
                with self.assertRaises(RuntimeError):
                    sandbox.guard()
                connect.assert_not_called()

    def test_search_allowlist_is_exact_and_get_only(self):
        sys.path.insert(0, str(sandbox.ROOT / 'deploy/arm2-windows-uat'))
        allowed = sandbox.allowed

        for catalog in ['vehicles', 'types', 'counterparties', 'organizations']:
            self.assertTrue(allowed('GET', f'/api/arm2/{catalog}/search'))
            self.assertFalse(allowed('POST', f'/api/arm2/{catalog}/search'))
            self.assertFalse(allowed('PUT', f'/api/arm2/{catalog}/search'))
        self.assertFalse(allowed('GET', '/api/arm2/documents/search'))
        self.assertFalse(allowed('GET', '/api/arm2/vehicles/search/anything'))
        self.assertFalse(allowed('POST', '/api/arm2/vehicles/VEH-000001/assign-owner'))


if __name__ == '__main__':
    unittest.main()
