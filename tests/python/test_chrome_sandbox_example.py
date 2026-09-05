"""Keep the Chrome sandbox example loadable without requiring Docker in unit tests."""

import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from harnest.bundle import compile_application
from harnest.context import activate_context, create_agent_context
from harnest.context_sandboxes import SandboxRegistry
from harnest.plugins import release_runtime_plugins
from harnest.sandbox import Sandbox, SandboxResult


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "chrome-sandbox"
_DOCKER_EXTENSION = _ROOT / "official-extensions" / "docker"


class RecordingBackend:
    """Return a browser-shaped result while retaining submitted code for inspection."""

    def __init__(self) -> None:
        self.code = ""

    def execute(self, request):
        """Record one request without starting Docker or making a network call."""
        self.code = request.code
        return SandboxResult(
            stdout=json.dumps(
                {
                    "url": "https://example.com/",
                    "status": 200,
                    "title": "Example Domain",
                    "text": "Example Domain",
                }
            )
        )


class ChromeSandboxExampleTests(unittest.TestCase):
    def _tool(self):
        """Load the authored tool from the hyphenated standalone example folder."""
        path = _EXAMPLE / "tools" / "browse_page.py"
        spec = importlib.util.spec_from_file_location("harnest_chrome_example_tool", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.browse_page

    def _context(self, backend):
        """Grant the fake provider through the same named capability used at runtime."""
        registry = SandboxRegistry(
            {"chrome_researcher": {"chrome": Sandbox.provider(lambda: backend)}}
        )
        return create_agent_context(
            framework="adk",
            agent_name="chrome_researcher",
            invocation_id="invocation",
            user_id="user",
            session_id="session",
            metadata={},
            resources={},
            sandbox_registry=registry,
        )

    def test_example_compiles_with_named_chrome_grant(self):
        """Exercise the documented extension install before compiling the agent."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "chrome-sandbox"
            shutil.copytree(_EXAMPLE, source)
            installed = source / "extensions" / "docker"
            installed.mkdir(parents=True)
            for name in ("extension.py", "extension.yaml", "pyproject.toml"):
                shutil.copy2(_DOCKER_EXTENSION / name, installed / name)
            shutil.copytree(_DOCKER_EXTENSION / "lib", installed / "lib")
            application = compile_application(
                source, entrypoint="agent:root_agent", framework="adk"
            )
            try:
                runtime = application.sandbox_registry._runtime(
                    "chrome_researcher", "chrome"
                )
                self.assertEqual(runtime.definition.backend, "docker")
            finally:
                release_runtime_plugins(
                    tuple(plugin.descriptor for plugin in application.plugins)
                )

    def test_tool_serializes_url_and_returns_browser_fields(self):
        """Use the managed sandbox handle while substituting only the provider backend."""
        backend = RecordingBackend()
        with activate_context(self._context(backend)):
            result = asyncio.run(self._tool()("https://example.com/docs?q=browser"))
        self.assertEqual(result["title"], "Example Domain")
        self.assertIn(
            'json.loads(\'{"url": "https://example.com/docs?q=browser", '
            '"allowed_hosts": ["example.com", "playwright.dev"]}\')',
            backend.code,
        )
        self.assertIn("playwright.chromium.launch", backend.code)
        self.assertIn('browser_context.route("**/*", admit)', backend.code)

    def test_tool_rejects_unapproved_hosts_before_provider_start(self):
        """Keep broad network access behind an authored exact-host allowlist."""
        backend = RecordingBackend()
        with activate_context(self._context(backend)):
            result = asyncio.run(self._tool()("http://127.0.0.1/private"))
        self.assertEqual(
            result, {"error": "That URL is not on this agent's approved host list."}
        )
        self.assertEqual(backend.code, "")


if __name__ == "__main__":
    unittest.main()
