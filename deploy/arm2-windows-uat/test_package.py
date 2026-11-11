"""Local configuration/policy tests. These NEVER launch Docker, ARM1 or ARM2."""

import ast
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from uat_policy import DATABASE, PACKAGE, SOURCE, allowed, readiness, validate_environment  # noqa: E402


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load((HERE / "compose.yaml").read_text(encoding="utf-8"))

    def test_closed_service_graph_and_egress(self):
        self.assertEqual(set(self.compose["services"]), {"uat-db", "uat-migrate", "uat-api", "uat-web"})
        self.assertEqual(self.compose["name"], "arm2-uat-r2-001")
        self.assertTrue(self.compose["networks"]["uat-net"]["internal"])
        for service in self.compose["services"].values():
            self.assertEqual(service["networks"], ["uat-net"])
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("env_file", service)
            self.assertNotIn("network_mode", service)
            self.assertNotIn("privileged", service)
            self.assertEqual(service["labels"]["io.arm2.uat.source"], SOURCE)
            self.assertTrue(service["container_name"].startswith("arm2-uat-r2-001-"))
            for mount in service.get("volumes", []):
                self.assertIn(mount.split(":")[0], {"uat-db", "uat-data"})
        self.assertEqual(self.compose["services"]["uat-web"]["ports"], ["127.0.0.1:18082:80"])
        for name in ("uat-db", "uat-api", "uat-migrate"):
            self.assertNotIn("ports", self.compose["services"][name])
        for volume in self.compose["volumes"].values():
            self.assertNotIn("external", volume)
            self.assertTrue(volume["name"].startswith("arm2-uat-r2-001-"))

    def test_migrations_before_readiness_and_no_default_entrypoint(self):
        services = self.compose["services"]
        self.assertEqual(services["uat-api"]["depends_on"]["uat-migrate"]["condition"], "service_completed_successfully")
        self.assertEqual(services["uat-web"]["depends_on"]["uat-api"]["condition"], "service_healthy")
        self.assertEqual(services["uat-migrate"]["command"], ["python", "/uat/uat_control.py", "migrate"])
        self.assertIn("uat_entrypoint:app", services["uat-api"]["command"])
        control = (HERE / "uat_control.py").read_text()
        self.assertIn('command.upgrade(Config("/service/alembic.ini"), REVISION)', control)
        self.assertIn('"create-admin"', control)
        self.assertNotIn('"subscribe"', control)

    def test_environment_cannot_inherit_working_tokens(self):
        env = self.compose["services"]["uat-api"]["environment"]
        self.assertEqual(env["MAX_BOT_TOKEN"], "")
        self.assertEqual(env["YANDEX_DISK_TOKEN"], "")
        self.assertEqual(env["ML_AUTO_TRAIN"], "false")
        self.assertEqual(env["ORGANIZATIONS_ENABLED"], "false")
        self.assertEqual(env["DATA_DIR"], "/data")
        self.assertNotIn("host.docker.internal", env["DATABASE_URL"])

    def valid_environment(self):
        env = dict(self.compose["services"]["uat-api"]["environment"])
        env["DATABASE_URL"] = "postgresql+psycopg://arm2_uat:" + "a" * 64 + "@uat-db:5432/arm2_uat_r2_001"
        env["MAX_WEBHOOK_SECRET"] = "b" * 64
        return env

    def test_fail_closed_runtime_config(self):
        env = self.valid_environment()
        with patch.dict(os.environ, env, clear=True):
            validate_environment()
        for key, bad in {"DATABASE_URL": "postgresql://production/real", "MAX_BOT_TOKEN": "real-token",
                         "YANDEX_DISK_TOKEN": "real-token", "ORGANIZATIONS_ENABLED": "true",
                         "ML_AUTO_TRAIN": "true", "DATA_DIR": "C:/data", "COOKIE_SECURE": "true"}.items():
            with self.subTest(key=key), patch.dict(os.environ, env | {key: bad}, clear=True), self.assertRaises(RuntimeError):
                validate_environment()

    def test_only_required_authenticated_api_paths_reach_approved_app(self):
        for method, path in [
            ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("GET", "/api/auth/me"),
            ("GET", "/api/meta"), ("GET", "/api/platform/status"), ("GET", "/api/users"), ("POST", "/api/users"), ("PATCH", "/api/users/2"),
            ("POST", "/api/arm2/types"), ("PUT", "/api/arm2/vehicles/VEH-000001"),
            ("POST", "/api/arm2/counterparties/CP-000001/representatives"),
            ("PUT", "/api/arm2/organizations/ORG-000001/representatives/ORGREP-000001"),
            ("GET", "/api/arm2/history/CPREP/CPREP-000001"), ("GET", "/api/arm2/history/events/1"),
        ]:
            with self.subTest(path=path):
                self.assertTrue(allowed(method, path))
        for path in ["/api/webhooks/max", "/api/integration/v1/photos", "/api/disk-folders", "/api/photos",
                     "/api/jobs/1/retry", "/api/receiver/status", "/api/exchange", "/api/platform/organizations",
                     "/api/public/appeals", "/org/123/api/arm2/types", "/api/arm2/history", "/api/users/1/password"]:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                self.assertFalse(allowed(method, path), (method, path))
        self.assertFalse(allowed("DELETE", "/api/users/1"))
        self.assertFalse(allowed("DELETE", "/api/arm2/types/TYPE-000001"))

    def test_health_identifies_actual_database_and_schema(self):
        class Connection:
            def __init__(self, database, revision):
                self.values = iter([database, revision])

            def scalar(self, _query):
                return next(self.values)

        self.assertEqual(readiness(Connection(DATABASE, "0061"))["source_sha"], SOURCE)
        for database, revision in [("other", "0061"), (DATABASE, "0060")]:
            with self.assertRaises(RuntimeError):
                readiness(Connection(database, revision))

    def test_approved_api_has_no_startup_scheduler(self):
        tree = ast.parse((ROOT / "backend/app/api.py").read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "FastAPI":
                self.assertFalse({key.arg for key in node.keywords} & {"lifespan", "on_startup"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"on_event", "create_task", "add_event_handler"})
        # API imports receiver helpers, not its guarded polling main loop.
        receiver = (ROOT / "backend/app/receiver.py").read_text(encoding="utf-8-sig")
        self.assertIn('if __name__ == "__main__":', receiver)

    def test_actual_shell_bootstrap_and_user_editor_contracts_are_supported(self):
        application = (ROOT / "frontend/src/Application.tsx").read_text(encoding="utf-8-sig")
        self.assertIn("'/platform/status'", application)
        self.assertTrue(allowed("GET", "/api/platform/status"))
        organizations = (ROOT / "backend/app/organizations.py").read_text(encoding="utf-8-sig")
        self.assertIn('return {"enabled": base_settings().organizations_enabled}', organizations)
        editor = (ROOT / "frontend/src/Administration.tsx").read_text(encoding="utf-8-sig")
        self.assertIn("editing?'PATCH':'POST'", editor)
        self.assertTrue(allowed("PATCH", "/api/users/2"))
        smoke = (HERE / "smoke.py").read_text()
        self.assertIn('trust_env=False', smoke)
        self.assertIn('getpass.getpass', smoke)
        self.assertIn('request("GET", rep_path)', smoke)
        self.assertNotIn('subprocess', smoke)

    def test_identity_gate_and_scoped_cleanup_are_not_bypass_defaults(self):
        script = (HERE / "uat.ps1").read_text(encoding="utf-8-sig")
        for forbidden in ["docker system prune", "down -v", "Remove-Item", "Stop-Service", "Restart-Service", "wsl --install", "Start-Process", "C:\\ARM-MVP", "C:\\ARM\\"]:
            self.assertNotIn(forbidden, script)
        for required in ["--project-name", "--project-directory", "--env-file", "--file", "desktop-linux",
                         "Check-Ownership", "Check-Labels", "BackupRestoreVerified", "Arm1BaselinePassed",
                         "ReparsePoint", "DELETE arm2-uat-r2-001 TEST DATA", "--no-build"]:
            self.assertIn(required, script)
        gate = json.loads((HERE / "gate.example.json").read_text())
        self.assertFalse(gate["Arm1BaselinePassed"])
        self.assertFalse(gate["BackupRestoreVerified"])
        self.assertEqual(json.loads((HERE / "instance.json").read_text())["package"], PACKAGE)

    @unittest.skipUnless(shutil.which("powershell.exe"), "PowerShell parser not available")
    def test_powershell_syntax_without_execution(self):
        for filename in ("uat.ps1", "inventory.ps1"):
            environment = os.environ | {"ARM2_UAT_PARSE_FILE": str(HERE / filename)}
            command = "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:ARM2_UAT_PARSE_FILE,[ref]$t,[ref]$e);if($e){$e|ForEach-Object{$_.Message};exit 1}"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                    check=False, env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
