"""Default-off deployment boundaries across Python and authenticated Studio requests."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from harnest._features import DEPLOYMENT_DISABLED, DEPLOYMENT_FLAG, deployment_enabled
from harnest.provisioner import Provisioner
from harnest.provisioner_cli import initialize
from harnest.provisioner_config import ProvisionError
from test_agent_builder import _BuilderFixture

OPERATIONS = ("init", "plan", "apply", "status", "stop", "remove", "history", "rollback")


class DeploymentFlagTests(unittest.TestCase):
    """Require explicit process opt-in before any deployment work."""

    def test_only_explicit_true_enables_deployment(self):
        """Missing and malformed settings fail closed with identical CLI/server semantics."""
        for value in (None, "", "false", "FALSE", "0", "1", "yes", "tru", "true", " TRUE "):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ[DEPLOYMENT_FLAG] = value
                self.assertEqual(deployment_enabled(), value in ("true", " TRUE "))

    def test_python_cli_blocks_every_operation_without_side_effects(self):
        """The module CLI cannot bypass the native CLI guard, including starter generation."""
        from harnest.provisioner_cli import main

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            for operation in OPERATIONS:
                output = io.StringIO()
                with self.subTest(operation=operation), contextlib.redirect_stderr(output):
                    self.assertEqual(main([operation, "--project", directory]), 1)
                    self.assertIn(DEPLOYMENT_DISABLED, output.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaisesRegex(ProvisionError, "Deployment is disabled"):
                initialize(Path(directory))

    def test_direct_provisioner_rechecks_flag_before_work(self):
        """Objects created while enabled cannot mutate state after opt-in is withdrawn."""
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {DEPLOYMENT_FLAG: "true"}):
            runner = Mock()
            service = Provisioner(Path(directory), runner=runner)
            os.environ.pop(DEPLOYMENT_FLAG)
            actions = (service.plan, service.apply, service.status, service.history, service.recorded,
                       lambda: service.rollback(1), lambda: service.control("remove"), lambda: service.control("stop"))
            for action in actions:
                with self.assertRaisesRegex(ProvisionError, "Deployment is disabled"):
                    action()
            runner.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_module_process_inherits_opt_in(self):
        """Exercise the actual child-process contract used by the native CLI."""
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
            env.pop(DEPLOYMENT_FLAG, None)
            argv = [sys.executable, "-m", "harnest.provisioner_cli", "init", "--project", directory]
            disabled = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(disabled.returncode, 1)
            self.assertIn(DEPLOYMENT_DISABLED, disabled.stderr)
            env[DEPLOYMENT_FLAG] = "true"
            enabled = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            self.assertTrue((Path(directory) / "harnest-deployment.yaml").exists())


class StudioDeploymentFlagTests(_BuilderFixture):
    """Keep deployment invisible and unavailable even through direct HTTP requests."""

    def setUp(self):
        """Start Studio with the default regardless of the test runner's environment."""
        self.enterContext(patch.dict(os.environ))
        os.environ.pop(DEPLOYMENT_FLAG, None)
        super().setUp()

    def test_disabled_routes_and_commands_never_start_jobs(self):
        """Reject every lifecycle route before reading manifests or launching processes."""
        self.assertEqual(self.client.get("/api/workspace").json()["features"], {"deployment": False})
        with patch.object(self.app.state.jobs, "start") as start:
            for operation in OPERATIONS:
                result = self.client.post("/api/command", json={"action": "provision", "operation": operation, "project": "sample"})
                self.assertEqual(result.status_code, 403, result.text)
                self.assertEqual(result.json()["detail"], DEPLOYMENT_DISABLED)
            for route in ("inspect", "overview", "history"):
                result = self.client.get("/api/deployment/" + route, params={"project": "sample"})
                self.assertEqual(result.status_code, 403, result.text)
            result = self.client.post("/api/deployment/propose", json={})
            self.assertEqual(result.status_code, 403, result.text)
            start.assert_not_called()
        self.assertFalse((self.project / ".harnest").exists())

    def test_build_and_serve_remain_available_and_flag_is_server_owned(self):
        """Browser data cannot enable deployment; normal local commands still work."""
        with patch.object(self.app.state.jobs, "start", return_value={"id": "test"}) as start:
            for action in ("compile", "serve", "test"):
                result = self.client.post("/api/command", json={"action": action, "project": "sample"})
                self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(start.call_count, 3)
        result = self.client.get("/api/workspace?deployment=true")
        self.assertFalse(result.json()["features"]["deployment"])
        with patch.dict(os.environ, {DEPLOYMENT_FLAG: "true"}):
            self.assertTrue(self.client.get("/api/workspace").json()["features"]["deployment"])
            self.assertEqual(self.client.get("/api/deployment/inspect?project=sample").status_code, 200)
