import asyncio
import os
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from harnest.agent import AgentDefinition
from harnest.mcp import MCPClient
from harnest.mcp_lifecycle import (
    MCPClientContext,
    MCPClientLifecycle,
    MCPHTTPClientOptions,
    _MCPClientLifecycleBinding,
    _MCPClientLifecycleController,
    close_mcp_lifecycles,
    mcp_lifecycle_bindings,
    start_mcp_lifecycles,
)


class _RecordingLifecycle(MCPClientLifecycle):
    """Record portable lifecycle calls without opening a network connection."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.options: list[MCPHTTPClientOptions] = []

    async def start(self, context: MCPClientContext) -> None:
        self.events.append(("start", context.framework))

    def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ) -> httpx.AsyncClient:
        self.events.append(("create", context.framework))
        self.options.append(options)
        return httpx.AsyncClient(
            headers=dict(options.headers),
            timeout=options.timeout,
            auth=options.auth,
        )

    async def close(self, context: MCPClientContext) -> None:
        self.events.append(("close", context.framework))


class _FailingLifecycle(MCPClientLifecycle):
    """Raise a secret-bearing error from one selected lifecycle hook."""

    def __init__(self, hook: str) -> None:
        self.hook = hook

    def start(self, context: MCPClientContext) -> None:
        del context
        if self.hook == "start":
            raise ValueError("gateway token is secret-value")

    def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ) -> httpx.AsyncClient:
        del options, context
        if self.hook == "create_http_client":
            raise ValueError("gateway token is secret-value")
        return httpx.AsyncClient()

    def close(self, context: MCPClientContext) -> None:
        del context
        if self.hook == "close":
            raise ValueError("gateway token is secret-value")


class _AsyncFactoryLifecycle(MCPClientLifecycle):
    """Model the unsupported async form of the adapter-owned client factory."""

    async def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ) -> httpx.AsyncClient:
        del options, context
        return httpx.AsyncClient()


class _PartialStartLifecycle(MCPClientLifecycle):
    """Allocate observable state before failing startup."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self, context: MCPClientContext) -> None:
        del context
        self.events.append("start")
        raise ValueError("partially initialized secret-value")

    async def close(self, context: MCPClientContext) -> None:
        del context
        self.events.append("close")


class _CancelledStartLifecycle(_PartialStartLifecycle):
    """Expose cancellation cleanup and redaction without cancelling the test."""

    async def start(self, context: MCPClientContext) -> None:
        del context
        self.events.append("start")
        raise asyncio.CancelledError("cancelled with secret-value")


class _RecordingType:
    """Stand in for ADK classes while preserving constructor keyword arguments."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.__dict__.update(kwargs)


def _binding(
    lifecycle: MCPClientLifecycle,
    *,
    framework: str = "adk",
) -> _MCPClientLifecycleBinding:
    """Build a bound controller for lifecycle contract tests."""

    return _MCPClientLifecycleBinding(
        _MCPClientLifecycleController(lifecycle),
        MCPClientContext(
            name="catalog",
            transport="streamable-http",
            framework=framework,  # type: ignore[arg-type]
            url="https://gateway.example/mcp?token=secret-value",
        ),
    )


class MCPClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_client_supports_environment_socks_proxies(self):
        """Default MCP sessions inherit uppercase and lowercase SOCKS proxies."""

        lifecycle = MCPClientLifecycle()
        options = MCPHTTPClientOptions(headers={})
        context = MCPClientContext(
            name="catalog",
            transport="streamable-http",
            framework="adk",
            url="https://gateway.example/mcp",
        )
        for variable in ("ALL_PROXY", "all_proxy"):
            with self.subTest(variable=variable), patch.dict(
                os.environ,
                {variable: "socks5h://proxy-user:proxy-secret@127.0.0.1:1080"},
                clear=True,
            ):
                client = lifecycle.create_http_client(options, context)
                self.assertIsInstance(client, httpx.AsyncClient)
                await client.aclose()

    async def test_default_client_redacts_proxy_construction_failures(self):
        """Provider diagnostics cannot disclose credentials embedded in proxy URLs."""

        options = MCPHTTPClientOptions(headers={})
        context = MCPClientContext(
            name="catalog",
            transport="streamable-http",
            framework="adk",
            url="https://gateway.example/mcp",
        )
        with patch(
            "mcp.shared._httpx_utils.create_mcp_http_client",
            side_effect=ValueError(
                "invalid socks5://proxy-user:proxy-secret@127.0.0.1:1080"
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                MCPClientLifecycle().create_http_client(options, context)

        self.assertEqual(
            str(caught.exception),
            "MCP HTTP client construction failed with ValueError",
        )
        self.assertNotIn("proxy-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    async def test_lifecycle_requires_a_remote_http_transport(self):
        with self.assertRaisesRegex(ValueError, "requires an HTTP transport"):
            MCPClient.stdio("python", "server.py", lifecycle=MCPClientLifecycle())
        with self.assertRaisesRegex(TypeError, "must extend MCPClientLifecycle"):
            MCPClient.sse(
                "https://gateway.example/sse",
                lifecycle=object(),  # type: ignore[arg-type]
            )

    async def test_start_create_and_close_are_owned_at_the_right_scopes(self):
        lifecycle = _RecordingLifecycle()
        binding = _binding(lifecycle)

        await start_mcp_lifecycles((binding, binding))
        factory = binding.client_factory()
        client = factory(
            headers={"Authorization": "Bearer secret-value"},
            timeout=2.0,
        )

        self.assertIsInstance(client, httpx.AsyncClient)
        self.assertEqual(
            lifecycle.events,
            [("start", "adk"), ("create", "adk")],
        )
        self.assertEqual(
            lifecycle.options[0].headers["Authorization"],
            "Bearer secret-value",
        )
        with self.assertRaises(TypeError):
            lifecycle.options[0].headers["X-New"] = "blocked"  # type: ignore[index]
        self.assertNotIn("secret-value", repr(lifecycle.options[0]))
        self.assertNotIn("secret-value", repr(binding.context))

        # The framework closes each session client before Harnest closes the
        # application-level resources held by the authored lifecycle.
        await client.aclose()
        await close_mcp_lifecycles((binding, binding))
        self.assertEqual(
            lifecycle.events,
            [("start", "adk"), ("create", "adk"), ("close", "adk")],
        )

    async def test_create_http_client_must_be_synchronous(self):
        binding = _binding(_AsyncFactoryLifecycle())
        await binding.start()

        with self.assertRaisesRegex(TypeError, "must be synchronous"):
            binding.client_factory()()

        await binding.close()

    async def test_hook_errors_are_redacted(self):
        for hook in ("start", "create_http_client", "close"):
            with self.subTest(hook=hook):
                binding = _binding(_FailingLifecycle(hook))
                if hook == "start":
                    operation = binding.start
                else:
                    await binding.start()
                    operation = (
                        binding.client_factory()
                        if hook == "create_http_client"
                        else binding.close
                    )

                with self.assertRaises(RuntimeError) as caught:
                    result = operation()
                    if hasattr(result, "__await__"):
                        await result

                self.assertEqual(
                    str(caught.exception),
                    f"MCP lifecycle {hook} failed with ValueError",
                )
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)
                if hook == "create_http_client":
                    await binding.close()

    async def test_failed_start_attempts_cleanup_without_retaining_secrets(self):
        lifecycle = _PartialStartLifecycle()
        binding = _binding(lifecycle)

        with self.assertRaisesRegex(RuntimeError, "start failed") as caught:
            await binding.start()

        self.assertEqual(lifecycle.events, ["start", "close"])
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_cancelled_start_cleans_up_and_preserves_cancellation(self):
        lifecycle = _CancelledStartLifecycle()
        binding = _binding(lifecycle)

        with self.assertRaises(asyncio.CancelledError) as caught:
            await binding.start()

        self.assertEqual(lifecycle.events, ["start", "close"])
        self.assertEqual(str(caught.exception), "")
        self.assertIsNone(caught.exception.__context__)

    async def test_one_lifecycle_instance_cannot_cross_connections(self):
        lifecycle = _RecordingLifecycle()
        first = MCPClient.sse(
            "https://first.example/sse", lifecycle=lifecycle
        )._lifecycle_binding("langgraph")
        second = MCPClient.sse(
            "https://second.example/sse", lifecycle=lifecycle
        )._lifecycle_binding("langgraph")
        assert first is not None and second is not None

        with self.assertRaisesRegex(RuntimeError, "cannot be shared"):
            await start_mcp_lifecycles((first, second))

        self.assertEqual(lifecycle.events, [])

    async def test_adk_connection_receives_portable_http_client_factory(self):
        lifecycle = _RecordingLifecycle()
        client = MCPClient.sse(
            "https://gateway.example/sse",
            headers={"X-Gateway": "configured"},
            lifecycle=lifecycle,
        )
        classes = (
            _RecordingType,
            _RecordingType,
            _RecordingType,
            _RecordingType,
            _RecordingType,
        )

        with patch("harnest.mcp._adk_mcp_classes", return_value=classes):
            toolset = client.to_adk_toolset()

        bindings = mcp_lifecycle_bindings(toolset)
        self.assertEqual(len(bindings), 1)
        await start_mcp_lifecycles(bindings)
        factory = toolset.connection_params.httpx_client_factory
        http_client = factory(headers={"X-Adapter": "adk"}, timeout=1.0)

        self.assertIsInstance(http_client, httpx.AsyncClient)
        self.assertEqual(lifecycle.events[:2], [("start", "adk"), ("create", "adk")])
        await http_client.aclose()
        await close_mcp_lifecycles(bindings)
        self.assertEqual(lifecycle.events[-1], ("close", "adk"))

    async def test_managed_adk_agent_owns_its_mcp_lifecycle(self):
        lifecycle = _RecordingLifecycle()
        built = AgentDefinition(
            name="gateway_agent",
            model="openai:test",
            instruction="Use the gateway.",
            mcp=(
                MCPClient.streamable_http(
                    "https://gateway.example/mcp",
                    lifecycle=lifecycle,
                ),
            ),
        ).build()

        bindings = mcp_lifecycle_bindings(built)
        self.assertEqual(len(bindings), 1)
        await start_mcp_lifecycles(bindings)
        await close_mcp_lifecycles(bindings)
        self.assertEqual(
            lifecycle.events,
            [("start", "adk"), ("close", "adk")],
        )

    async def test_langgraph_connection_receives_portable_http_client_factory(self):
        lifecycle = _RecordingLifecycle()
        client = MCPClient.streamable_http(
            "https://gateway.example/mcp",
            lifecycle=lifecycle,
        )
        binding = client._lifecycle_binding("langgraph")
        self.assertIsNotNone(binding)
        assert binding is not None
        await binding.start()

        connection = client.to_langgraph_connection()
        factory = connection["httpx_client_factory"]
        http_client = factory(headers={"X-Adapter": "langgraph"}, timeout=1.0)

        self.assertIsInstance(http_client, httpx.AsyncClient)
        self.assertEqual(
            lifecycle.events[:2],
            [("start", "langgraph"), ("create", "langgraph")],
        )
        await http_client.aclose()
        await binding.close()
        self.assertEqual(lifecycle.events[-1], ("close", "langgraph"))


if __name__ == "__main__":
    unittest.main()
