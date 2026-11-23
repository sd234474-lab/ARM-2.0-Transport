import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from collect import fingerprint, sqlite_metadata
from render import render, validate
from verify import compare

HERE = Path(__file__).resolve().parent


def example():
    return {"schema_version": 1, "all_prototypes": True, "summary": "All prototypes",
            "platforms": [{"id": "p-test", "name": "Test", "path": "X:\\Example\\Platform", "version": "commit", "runtime": "Python", "state": "Not launched"}],
            "configurations": [{"id": "c-test", "name": "Embedded", "platform": "p-test", "format": "migrations", "version": "22", "compatibility": "not runtime tested"}],
            "infobases": [{"id": "b-test", "name": "Example", "kind": "SQLite", "path": "X:\\Example\\data.db", "schema": "22", "configuration": "c-test", "environment": "Prototype", "state": "metadata only"}],
            "instances": [{"id": "i-test", "name": "Choice", "platform": "p-test", "configuration": "c-test", "infobases": ["b-test"], "launcher": "X:\\Example\\old.cmd", "url": "NOT ESTABLISHED", "state": "No generated launch command"}],
            "storage": [], "changes": [], "navigation": []}


class CatalogueTests(unittest.TestCase):
    def test_sqlite_metadata_does_not_read_subject_or_credential_rows_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("CREATE TABLE schema_migrations(version TEXT); INSERT INTO schema_migrations VALUES('0022'); CREATE TABLE users(password TEXT); INSERT INTO users VALUES('DO-NOT-COPY-CREDENTIAL'); CREATE TABLE customers(name TEXT); INSERT INTO customers VALUES('DO-NOT-COPY-PERSON');")
                db.commit()
            before = fingerprint(path)
            metadata = sqlite_metadata(path)
            self.assertEqual(metadata["schema"]["latest"], "0022")
            self.assertNotIn("DO-NOT-COPY", json.dumps(metadata))
            self.assertEqual(fingerprint(path), before)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["sample.db"])

    def test_live_wal_and_partial_not_mistaken_for_consistent_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            path.write_bytes(b"data")
            Path(str(path) + "-wal").write_bytes(b"pending")
            self.assertIn("WAL", sqlite_metadata(path)["state"])
            partial = Path(directory) / "sample.db.abc.partial"
            partial.write_bytes(b"not a database")
            self.assertIn("not opened", sqlite_metadata(partial)["state"])

    def test_platform_configuration_and_database_are_independent_and_links_validated(self):
        registry = example()
        validate(registry)
        registry["instances"][0]["infobases"] = ["missing"]
        with self.assertRaises(ValueError):
            validate(registry)

    def test_commands_and_secrets_cannot_be_added_as_registry_columns(self):
        for key in ("password", "command", "arguments", "execute"):
            registry = example()
            registry["instances"][0][key] = "unsafe"
            with self.assertRaises(ValueError):
                validate(registry)

    def test_navigation_cannot_be_a_remote_or_relative_target(self):
        for path in ("https://example.test/", "../Data", "\\\\server\\data", "X:\\Example\\..\\Data"):
            registry = example()
            registry["navigation"] = [{"id": "link", "category": "01_PLATFORMS", "target": path}]
            with self.assertRaises(ValueError):
                validate(registry)

    def test_html_escapes_metadata_and_does_not_generate_executable_links(self):
        registry = example()
        registry["platforms"][0]["name"] = '<script>alert("x")</script>'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new"
            render(registry, {"observed_at": "now", "protected_files": []}, target)
            page = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn('<script>alert', page)
            self.assertNotIn('href="X:', page)
            self.assertNotIn('file:///', page)
            self.assertEqual(json.loads((target / "registry.json").read_text())["instances"][0]["infobases"], ["b-test"])
            with self.assertRaises(FileExistsError):
                render(registry, {}, target)

    def test_protection_evidence_detects_external_changes_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.cmd"
            path.write_text("original")
            before = fingerprint(path)
            self.assertEqual(compare([before])["unchanged"], [str(path)])
            path.write_text("external change")
            self.assertEqual(compare([before])["changed"], [str(path)])
            self.assertEqual(path.read_text(), "external change")

    def test_installer_only_creates_new_catalogue_and_folder_navigation(self):
        script = (HERE / "install.ps1").read_text(encoding="utf-8")
        for denied in ("Remove-Item", "Move-Item", "Stop-Process", "Start-Process", "Invoke-Item", "Set-ItemProperty", "Start-Service", "Copy-Item -Force"):
            self.assertNotIn(denied, script)
        for guard in ("Refusing to overwrite existing 00_START", "Refusing to overwrite existing shortcut", "-PathType Container", "ReparsePoint", "Shortcut collision", "Only catalogue documents"):
            self.assertIn(guard, script)
        pattern = re.search(r"nav\.target -notmatch '([^']+)'", script).group(1)
        self.assertIsNotNone(re.match(pattern, "C:\\Folder"))

    @unittest.skipUnless(shutil.which("powershell.exe"), "PowerShell parser unavailable")
    def test_powershell_parses_without_running_installations(self):
        for filename in ("install.ps1", "snapshot.ps1"):
            environment = os.environ | {"ARM_CATALOG_PARSE": str(HERE / filename)}
            command = "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:ARM_CATALOG_PARSE,[ref]$t,[ref]$e);if($e){$e|ForEach-Object{$_.Message};exit 1}"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                    env=environment, capture_output=True, text=True, check=False, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
