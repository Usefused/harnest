"""Verify the removed shorthand fails before either framework grants execution."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harnest.bundle import BundleImportError, compile_application


class SandboxFrameworkTests(unittest.TestCase):
    def test_singular_argument_is_rejected_by_both_compilers(self):
        """Old source receives a clear constructor failure without starting a provider."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "instructions.md").write_text("Test")
                (root / "agent.py").write_text(
                    "from harnest import Agent, Sandbox\n"
                    "root_agent = Agent(name='test', model='test/model', "
                    "sandbox=Sandbox.provider(lambda: None))\n"
                )
                with self.assertRaisesRegex(BundleImportError, "unexpected keyword argument.*sandbox"):
                    compile_application(root, entrypoint="agent:root_agent", framework=framework)

    def test_portable_provider_imports_no_framework_executor(self):
        """A fresh process proves provider setup does not import a framework."""
        code = '''
import importlib.abc
import sys
class NoFramework(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("google.adk", "langgraph", "langchain")):
            raise ModuleNotFoundError("framework deliberately unavailable: " + fullname)
sys.meta_path.insert(0, NoFramework())
from harnest.sandbox import Sandbox
from harnest.sandbox import SandboxResult
class Backend:
    def execute(self, request):
        return SandboxResult(stdout="ok")
backend = Sandbox.provider(Backend).build()
assert backend is not None
assert not any(name.startswith(("google.adk", "langgraph", "langchain")) for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
