"""Narrow regression tests for cross-instance safety; no application DB fixtures."""
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import policy
import prepare
import run


class NativeSafetyTests(unittest.TestCase):
    def valid_env(self):
        return {"DATABASE_URL": f"postgresql+psycopg://arm2_uat:{'a' * 64}@127.0.0.1:55442/arm2_win_run_001",
                "APP_ENV": "test", "APP_ORIGIN": "http://127.0.0.1:18082", "COOKIE_SECURE": "false",
                "ORGANIZATIONS_ENABLED": "false", "MAX_BOT_TOKEN": "", "YANDEX_DISK_TOKEN": "",
                "ML_AUTO_TRAIN": "false", "ML_DOWNLOAD_BACKBONE": "false", "MAX_UPDATE_MODE": "webhook",
                "DATA_DIR": str(policy.ROOT / "data")}

    def test_reject_borrowed_database_or_integration_settings(self):
        good = self.valid_env()
        with patch("policy.config"), patch.dict(os.environ, good, clear=True):
            policy.validate_environment()
        for changed in [good["DATABASE_URL"].replace("55442", "55432"),
                        good["DATABASE_URL"].replace("127.0.0.1", "remote.invalid"),
                        good["DATABASE_URL"] + "?options=anything",
                        good["DATABASE_URL"] + "#fragment"]:
            with patch("policy.config"), patch.dict(os.environ, good | {"DATABASE_URL": changed}, clear=True):
                with self.assertRaises(RuntimeError):
                    policy.validate_environment()
        for key, value in [("MAX_BOT_TOKEN", "live"), ("APP_ENV", "production"), ("ORGANIZATIONS_ENABLED", "true")]:
            with patch("policy.config"), patch.dict(os.environ, good | {key: value}, clear=True):
                with self.assertRaises(RuntimeError):
                    policy.validate_environment()

    def test_network_guard_blocks_old_ports_and_external_dns_but_socketpair_works(self):
        code = '''import policy,socket
policy.deny_external_network()
a,b=socket.socketpair(); a.send(b'x'); assert b.recv(1)==b'x'; a.close(); b.close()
for host,port in [('127.0.0.1',55432),('127.0.0.1',8081),('192.0.2.1',443)]:
    try: socket.socket().connect((host,port))
    except OSError as e: assert 'blocked' in str(e)
    else: raise AssertionError('unexpected connect')
try: socket.getaddrinfo('external.invalid',443)
except OSError as e: assert 'blocked' in str(e)
else: raise AssertionError('unexpected DNS')
'''
        subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(__file__).parent, check=True)

    def test_pid_reuse_is_rejected_without_signal(self):
        fake = Mock()
        fake.create_time.return_value = 20
        fake.cmdline.return_value = [str(run.HERE / "run.py"), "api"]
        with patch("psutil.Process", return_value=fake), self.assertRaises(RuntimeError):
            run.owned_process({"pid": 123, "created": 19}, "api")
        fake.terminate.assert_not_called()

    def test_foreign_command_is_rejected_without_signal(self):
        fake = Mock()
        fake.create_time.return_value = 20
        fake.cmdline.return_value = ["another-application.py", "api"]
        with patch("psutil.Process", return_value=fake), self.assertRaises(RuntimeError):
            run.owned_process({"pid": 123, "created": 20}, "api")
        fake.terminate.assert_not_called()

    def test_console_exception_requires_own_parent_and_exact_windows_binary(self):
        parent, child = Mock(), Mock()
        parent.pid = 123
        parent.create_time.return_value = 10
        child.ppid.return_value = 123
        child.create_time.return_value = 11
        child.cmdline.return_value = ["conhost.exe", "0x4"]
        child.exe.return_value = str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe")
        self.assertTrue(run.is_own_console(child, parent))
        child.ppid.return_value = 999
        self.assertFalse(run.is_own_console(child, parent))
        child.ppid.return_value = 123
        child.exe.return_value = str(policy.ROOT / "untrusted/conhost.exe")
        self.assertFalse(run.is_own_console(child, parent))

    def test_archive_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("pgsql/../../outside.txt", "must not escape")
            with self.assertRaises(RuntimeError):
                prepare.extract(archive, "pgsql/", root / "runtime")
            self.assertFalse((root / "outside.txt").exists())

    def test_postmaster_same_binary_other_data_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "db-data"
            data.mkdir()
            (data / "postmaster.pid").write_text(f"123\n{data}\n10\n55442\n")
            fake = Mock()
            fake.exe.return_value = str(run.PG / "postgres.exe")
            fake.cmdline.return_value = [str(run.PG / "postgres.exe"), "-D", str(root / "other-data")]
            fake.create_time.return_value = 10
            with patch.object(run, "ROOT", root), patch("psutil.Process", return_value=fake):
                with self.assertRaises(RuntimeError):
                    run.pg_owned()
            fake.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
