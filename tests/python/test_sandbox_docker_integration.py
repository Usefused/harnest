"""Opt-in real Docker checks for kernel policy, status, and cross-call isolation."""

import os
import subprocess
import sys
import unittest

from harnest.sandbox import Sandbox, SandboxBudget, SandboxContext, SandboxRequest, SandboxStatus


@unittest.skipUnless(os.environ.get("HARNEST_TEST_DOCKER") == "1", "set HARNEST_TEST_DOCKER=1 for real Docker isolation checks")
class DockerIsolationTests(unittest.TestCase):
    def backend(self, **settings):
        """Register cleanup before asserting against an actual owned container."""
        result = Sandbox.container(image="python:3.12-slim", **settings).build()
        self.addCleanup(result.close)
        return result

    def test_kernel_resource_policy_and_unprivileged_read_only_root(self):
        """Verify actual Docker kernel settings rather than only constructor options."""
        backend = self.backend(scope="session", budget=SandboxBudget(cpu=0.5, memory_bytes=64 * 1024 * 1024, pids=16, scratch_bytes=1024 * 1024))
        identity = SandboxContext("worker", "turn", "alice", "session")
        result = backend.execute(SandboxRequest("import os; print(os.getuid()); open('/tmp/owned', 'w').write('ok')", context=identity))
        self.assertEqual(result.stdout.strip(), "65534")
        owner = next(iter(backend._scopes.values()))
        owner._guard.container.reload()
        host = owner._guard.container.attrs["HostConfig"]
        self.assertEqual(host["NanoCpus"], 500_000_000)
        self.assertEqual(host["Memory"], 64 * 1024 * 1024)
        self.assertEqual(host["MemorySwap"], host["Memory"])
        self.assertEqual(host["PidsLimit"], 16)
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["NetworkMode"], "none")
        self.assertIn("size=1048576", host["Tmpfs"]["/tmp"])
        denied = backend.execute(SandboxRequest("open('/escape', 'w').write('bad')", context=identity))
        self.assertEqual(denied.status, SandboxStatus.FAILED)
        self.assertIn("Read-only file system", denied.stderr)
        cleared = backend.execute(SandboxRequest("import os; print(os.path.exists('/tmp/owned'))", context=identity))
        self.assertEqual(cleared.stdout.strip(), "False")

    def test_timeout_output_budget_and_nonzero_exit_are_explicit(self):
        """Host enforcement survives Python code and distinguishes output from success."""
        backend = self.backend(timeout_seconds=2, max_output_bytes=1024)
        failed = backend.execute(SandboxRequest("raise SystemExit(7)"))
        self.assertEqual((failed.status, failed.exit_code, failed.stderr), (SandboxStatus.FAILED, 7, ""))
        authored_timeout_code = backend.execute(SandboxRequest("raise SystemExit(124)"))
        self.assertEqual((authored_timeout_code.status, authored_timeout_code.exit_code), (SandboxStatus.FAILED, 124))
        timed_out = backend.execute(SandboxRequest("while True: pass"))
        self.assertEqual(timed_out.status, SandboxStatus.TIMED_OUT)
        overflow = backend.execute(SandboxRequest("print('x' * 100000)"))
        self.assertEqual(overflow.status, SandboxStatus.OUTPUT_LIMIT_EXCEEDED)
        self.assertEqual(backend.execute(SandboxRequest("print('recovered')")).stdout.strip(), "recovered")

    def test_fresh_default_prevents_cross_call_file_access(self):
        """Neither the same nor a different user inherits default-scope scratch files."""
        backend = self.backend()
        backend.execute(SandboxRequest("open('/tmp/private', 'w').write('secret')"))
        self.assertIsNone(backend._executor)
        self.assertFalse(backend._scopes)
        result = backend.execute(SandboxRequest("import os; print(os.path.exists('/tmp/private'))"))
        self.assertEqual(result.stdout.strip(), "False")

    def test_real_execution_without_either_framework_importable(self):
        """Execute through Docker in a process where ADK/LangGraph cannot be imported."""
        code = """
import importlib.abc
import sys
class Unavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('google.adk', 'langgraph', 'langchain')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Unavailable())
from harnest.sandbox import Sandbox, SandboxRequest
backend = Sandbox.container(image='python:3.12-slim').build()
try:
    assert backend.execute(SandboxRequest('print(42)')).stdout.strip() == '42'
    assert not any(name.startswith(('google.adk', 'langgraph', 'langchain')) for name in sys.modules)
finally:
    backend.close()
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
