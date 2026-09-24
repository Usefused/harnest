"""Verify the desktop example's compile and process ownership without Docker."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from harnest.bundle import compile_application
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.context_session import invocation_session_context
from harnest.decisions import DecisionAction, DecisionOutcome
from harnest.lifecycle import LifecycleContext
from harnest.runtime_contract import InvocationRequest
from harnest.session import InMemorySessionStore
from harnest.tool_lifecycle import ToolCallRequest, ToolLifecycleContext


ROOT = Path(__file__).resolve().parents[2] / "examples" / "desktop-mcp-agent"


def load(name: str, path: Path):
    """Load an example module without making its hyphenated folder a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeContainer:
    """Record cleanup of exactly the container started for one instance."""

    def __init__(self):
        self.stopped = False
        self.removed = False

    def stop(self, *, timeout):
        """Record the bounded graceful stop."""
        self.stopped = timeout == 5

    def remove(self, *, force):
        """Record removal after stop."""
        self.removed = force


class DesktopExampleTests(unittest.TestCase):
    def test_google_navigation_is_allowed_by_default_with_optional_host_filter(self):
        """Keep normal browsing open while honoring an explicit deployment list."""
        policy = load("desktop_example_url_policy", ROOT / "desktop" / "url_policy.py")
        google = "https://www.google.com/search?q=cheap+flights+to+malta"
        self.assertTrue(policy.allowed_url(google, frozenset()))
        self.assertFalse(policy.allowed_url(google, frozenset({"example.com"})))
        self.assertTrue(policy.allowed_url("https://example.com", frozenset({"example.com"})))
        self.assertFalse(policy.allowed_url("file:///etc/passwd", frozenset()))

    def test_provision_shell_sets_deepseek_environment_without_changing_agent_source(self):
        """Keep DeepSeek defaults at the launcher boundary and preserve overrides."""
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "python3"
            shim.write_text("#!/bin/sh\nprintf '%s\\n' \"$OPENAI_MODEL\" \"$OPENAI_BASE_URL\" \"$OPENAI_API_KEY\"\n")
            shim.chmod(0o755)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"}}
            environment.update({"PATH": f"{directory}:{os.environ['PATH']}",
                                "DEEPSEEK_API_KEY": "example-key", "OPENAI_API_KEY": "unrelated-key",
                                "TYPESAFE_AI_KEY": "jev-key"})
            result = subprocess.run([str(ROOT / "provision.sh"), "alpha"], env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(),
                             ["deepseek-flash", "https://api.deepseek.com", "example-key"])
            environment.update({"OPENAI_MODEL": "custom-model", "OPENAI_BASE_URL": "https://models.test/v1",
                                "OPENAI_API_KEY": "custom-key"})
            result = subprocess.run([str(ROOT / "provision.sh"), "alpha"], env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(),
                             ["custom-model", "https://models.test/v1", "custom-key"])
            environment.pop("TYPESAFE_AI_KEY")
            result = subprocess.run([str(ROOT / "provision.sh"), "alpha"], env=environment,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("TYPESAFE_AI_KEY", result.stderr)

    def test_each_instance_card_uses_its_live_port(self):
        """Avoid advertising the example port for dynamically provisioned agents."""
        module = load("desktop_example_provision", ROOT / "provision.py")
        first = module._instance_project(24000)
        second = module._instance_project(24001)
        try:
            self.assertIn("http://127.0.0.1:24000", (first / "agent-card.yaml").read_text())
            self.assertIn("http://127.0.0.1:24001", (second / "agent-card.yaml").read_text())
            self.assertIn("http://127.0.0.1:1907", (ROOT / "agent-card.yaml").read_text())
        finally:
            shutil.rmtree(first)
            shutil.rmtree(second)

    def test_managed_agent_compiles_with_startup_resource_and_desktop_tools(self):
        """Keep Docker construction out of compilation and discover the GUI tools."""
        with patch.dict(os.environ, {"OPENAI_MODEL": "test", "OPENAI_BASE_URL": "https://models.test/v1"}):
            application = compile_application(ROOT, entrypoint="agent:root_agent", framework="adk")
        self.assertEqual(
            [item.phase for item in application.lifecycle_extensions],
            ["before_invoke", "before_tool", "after_invoke", "resource", "resource"],
        )
        self.assertEqual(
            {tool.__name__ for tool in application.managed_definition.tools},
            {"desktop_screenshot", "desktop_click", "desktop_type", "desktop_key", "browser_navigate"},
        )

    def test_jev_decision_controls_browser_tools_for_each_invocation(self):
        """Enforce Jev's route even if the model proposes a desktop tool."""
        module = load("desktop_example_gate", ROOT / "lifecycle" / "browser_gate.py")

        async def evaluate_case(action, invocation):
            """Exercise the authored hooks around one Jev decision."""
            request = InvocationRequest("Open example.com", "user", "session", invocation, {}, {})
            agent_context = LifecycleContext("adk", "desktop", invocation, "user", "session")
            tool_context = ToolLifecycleContext("adk", "desktop", invocation, "user", "session",
                                                "browser_navigate")
            evaluation = SimpleNamespace(outcome=DecisionOutcome(action), error=None)
            jev = AsyncMock(return_value=evaluation)
            session = SimpleNamespace(get=AsyncMock(return_value=[]), set=AsyncMock())
            with patch.dict(module.context.__dict__, {
                "decisions": SimpleNamespace(evaluate=jev),
                "session": SimpleNamespace(namespace=lambda _: session),
            }):
                routed = await module.decide_browser_use(agent_context, request)
            browser = await module.enforce_browser_use(
                tool_context, ToolCallRequest("browser_navigate", kwargs={"url": "https://example.com"})
            )
            unrelated = await module.enforce_browser_use(
                tool_context, ToolCallRequest("unrelated_tool")
            )
            module.clear_browser_use(agent_context, None)
            return routed, browser, unrelated, jev

        allowed, blocked, unrelated, jev = asyncio.run(
            evaluate_case(DecisionAction.BLOCK, "invoke-direct")
        )
        self.assertIn("answer directly", allowed.value.input)
        self.assertIn("Do not use browser", blocked.result)
        self.assertFalse(unrelated.replaces)
        jev.assert_awaited_once_with(
            "browser_use", {"request": "Open example.com", "prior_user_requests": []}
        )
        allowed, permitted, _, _ = asyncio.run(
            evaluate_case(DecisionAction.PROCEED, "invoke-browser")
        )
        self.assertIn("tools are allowed", allowed.value.input)
        self.assertFalse(permitted.replaces)

    def test_jev_uses_prior_requests_only_from_the_same_session(self):
        """A date follow-up keeps its flight-search context without crossing sessions."""
        module = load("desktop_example_multiturn_gate", ROOT / "lifecycle" / "browser_gate.py")
        seen = []

        async def evaluate(_name, state):
            """Model Jev's choice from the context actually sent by the hook."""
            seen.append(state)
            prior = state["prior_user_requests"]
            browser = "flight" in state["request"].lower() or any(
                "flight" in item.lower() for item in prior
            )
            action = DecisionAction.PROCEED if browser else DecisionAction.BLOCK
            return SimpleNamespace(outcome=DecisionOutcome(action), error=None)

        async def run():
            """Execute three turns against real, independently leased sessions."""
            store = InMemorySessionStore()
            await store.start()
            for session_id in ("flights", "other"):
                await store.create(session_id=session_id, user_id="user", state={})
            try:
                for invocation, session_id, message in (
                    ("first", "flights", "Find cheap flights to Malta"),
                    ("follow-up", "flights", "Check for 20-25th Oct"),
                    ("unrelated", "other", "Check for 20-25th Oct"),
                ):
                    active = create_agent_context(
                        framework="adk", agent_name="desktop", invocation_id=invocation,
                        user_id="user", session_id=session_id, metadata={}, resources={},
                    )
                    lifecycle_context = LifecycleContext(
                        "adk", "desktop", invocation, "user", session_id
                    )
                    request = InvocationRequest(message, "user", session_id, invocation, {}, {})
                    try:
                        with activate_context(active):
                            async with invocation_session_context(
                                store, framework="adk", user_id="user",
                                session_id=session_id, invocation_id=invocation,
                            ):
                                with patch.dict(module.context.__dict__, {
                                    "decisions": SimpleNamespace(evaluate=evaluate)
                                }):
                                    result = await module.decide_browser_use(lifecycle_context, request)
                        if invocation == "follow-up":
                            self.assertIn("tools are allowed", result.value.input)
                        if invocation == "unrelated":
                            self.assertIn("tools are unavailable", result.value.input)
                    finally:
                        module.clear_browser_use(lifecycle_context, None)
                        revoke_context(active)
            finally:
                await store.close()

        asyncio.run(run())
        self.assertEqual(seen[0]["prior_user_requests"], [])
        self.assertEqual(seen[1]["prior_user_requests"], ["Find cheap flights to Malta"])
        self.assertEqual(seen[2]["prior_user_requests"], [])

    def test_start_reuses_one_named_container_until_close(self):
        """Prove one provisioned agent owns one container across operations."""
        module = load("desktop_example_owner", ROOT / "lib" / "desktop.py")
        container = FakeContainer()
        created = []
        client = SimpleNamespace(
            containers=SimpleNamespace(run=lambda image, **options: (created.append((image, options)), container)[1]),
            close=lambda: None,
        )
        environment = {
            "HARNEST_DESKTOP_INSTANCE": "alpha",
            "HARNEST_DESKTOP_API_PORT": "24000",
            "HARNEST_DESKTOP_VIEWER_PORT": "24001",
            "HARNEST_DESKTOP_TOKEN": "secret-token",
            "DESKTOP_ALLOWED_HOSTS": "",
        }
        with patch.dict(os.environ, environment), patch("docker.from_env", return_value=client), patch.object(module, "_wait_ready"):
            owner = module.Desktop.start()
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0][1]["name"], "harnest-desktop-alpha")
            self.assertEqual(created[0][1]["ports"]["6080/tcp"], ("127.0.0.1", 24001))
            self.assertEqual(created[0][1]["environment"]["ALLOWED_HOSTS"], "")
            self.assertFalse(container.stopped)
            owner.close()
        self.assertTrue(container.stopped)
        self.assertTrue(container.removed)

    def test_mcp_bridge_returns_session_for_follow_up(self):
        """Keep the MCP result tied to Harnest's response and session contract."""
        from fastmcp import Client

        module = load("desktop_example_mcp", ROOT / "mcp_server.py")
        captured = []

        class HTTPClient:
            """Record the exact Harnest request sent by the MCP tool."""

            def __init__(self, **options):
                captured.append(options)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, path, *, json):
                captured.append((path, json))
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"status": "completed", "outputText": "Done", "sessionId": "s-1"},
                )

        async def invoke():
            """Exercise FastMCP's actual in-memory tool transport."""
            async with Client(module.mcp) as client:
                return await client.call_tool("ask_agent", {"prompt": "Open Chrome", "session_id": "s-1"})

        with patch.object(module.httpx, "AsyncClient", HTTPClient):
            result = asyncio.run(invoke())
        self.assertEqual(captured[-1], ("/responses", {"input": "Open Chrome", "sessionId": "s-1"}))
        self.assertEqual(
            json.loads(result.content[0].text),
            {"status": "completed", "text": "Done", "session_id": "s-1"},
        )


if __name__ == "__main__":
    unittest.main()
