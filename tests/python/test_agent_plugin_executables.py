"""Compiled source retains runnable package-local servers without special bits."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harnest.agent_plugin_runtime import PortableMCP
from harnest.bundle import _copy_agent_source
from harnest.mcp import MCPClient


@unittest.skipUnless(os.name == "posix", "requires POSIX executable permissions")
class AgentPluginExecutableTests(unittest.TestCase):
    """Exercise permission copying and the portable ./bin/server launch path."""

    def setUp(self):
        """Keep all source and copied artifact resources inside one owned workspace."""
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.source = self.root / "source"
        self.package = self.source / "plugins" / "starter"
        self.package.mkdir(parents=True)
        self.destination = self.root / "artifact-source"
        environment = patch.dict(os.environ, {"HARNEST_PLUGIN_DATA_DIR": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_copy_preserves_only_authored_execution_permission_bits(self):
        """Do not turn ordinary files into programs or copy privileged mode bits."""
        modes = (0o644, 0o755, 0o710, 0o4755, 0o2755, 0o1755)
        for mode in modes:
            path = self.package / f"mode-{mode:o}"
            path.write_text("permission fixture\n", encoding="utf-8")
            path.chmod(mode)
        _copy_agent_source(self.source, self.destination)
        for mode in modes:
            with self.subTest(mode=oct(mode)):
                copied = self.destination / "plugins" / "starter" / f"mode-{mode:o}"
                actual = stat.S_IMODE(copied.stat().st_mode)
                self.assertEqual(actual & 0o111, mode & 0o111)
                self.assertEqual(actual & 0o7000, 0)

    def test_copied_plugin_relative_server_can_execute(self):
        """Launch a copied executable through the standard portable command mapping."""
        executable = self.package / "bin" / "server"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\nprintf 'copied-server-ran'\n", encoding="utf-8")
        executable.chmod(0o755)
        _copy_agent_source(self.source, self.destination)
        copied_package = self.destination / "plugins" / "starter"
        portable = PortableMCP.create(copied_package, "starter", "proof")
        client = MCPClient(transport="stdio", command="./bin/server", portable=portable)
        configuration = portable.stdio(client)
        result = subprocess.run(
            [configuration["command"], *configuration["args"]],
            cwd=configuration["cwd"], capture_output=True, text=True, check=True,
            timeout=5,
        )
        self.assertEqual(result.stdout, "copied-server-ran")
