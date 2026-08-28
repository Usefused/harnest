import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harnest.context import ContextResourceError, context
from harnest.bundle import compile_application
from harnest.credentials import (
    Credential,
    CredentialProvider,
    CredentialProviderError,
    credentials,
)
from harnest.credentials_adk import AdkCredentialPlugin
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.neutral_runtime import AgentInfo, InvocationResult
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime import _create_adk_fastapi_app


class _Provider(CredentialProvider):
    def __init__(self):
        self.started = 0
        self.closed = 0
        self.requests = []

    async def start(self):
        self.started += 1

    async def resolve(self, request):
        self.requests.append(request)
        return Credential("private-token")

    async def close(self):
        self.closed += 1


class _Driver:
    def __init__(self):
        self.closed = False
        self.material = []
        self.provider_was_public = False

    @property
    def info(self):
        return AgentInfo(
            id="support",
            name="support",
            description="Support",
            card={},
            framework="adk",
            mode="managed",
        )

    async def _resolve(self):
        try:
            context.resource("credentials")
        except ContextResourceError:
            pass
        else:
            self.provider_was_public = True
        value = await credentials.resolve("billing", scopes=("read",))
        self.material.append(value.reveal())

    async def invoke(self, request):
        await self._resolve()
        return InvocationResult(
            text="ok",
            events=({"type": "message", "text": "ok"},),
            result=None,
            session_id=request.session_id,
            metadata={},
        )

    async def stream(self, _request):
        await self._resolve()
        yield {"type": "message", "text": "ok"}

    async def close(self):
        self.closed = True


def _request():
    from harnest.neutral_runtime import InvocationRequest

    return InvocationRequest(
        input="hello",
        user_id="user-1",
        session_id="session-1",
        invocation_id="invoke-1",
        metadata={},
        state_delta={},
    )


def _storage_extensions(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "sessions.py").write_text(
        "from harnest.lifecycle import lifecycle\n"
        "from harnest.session import InMemorySessionStore\n"
        "@lifecycle.session_store\n"
        "def sessions(): return InMemorySessionStore()\n",
        encoding="utf-8",
    )
    (root / "checkpoints.py").write_text(
        "from harnest.checkpoint import MemoryStore\n"
        "from harnest.lifecycle import lifecycle\n"
        "@lifecycle.checkpointer\n"
        "def checkpoints(): return MemoryStore()\n",
        encoding="utf-8",
    )


class CredentialLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_owns_provider_once_and_keeps_it_private(self):
        provider = _Provider()
        backend = _Driver()
        driver = ExtensionRuntimeDriver(
            backend, (), credential_provider=provider
        )

        await driver.invoke(_request())
        events = [event async for event in driver.stream(_request())]
        await driver.close()
        await driver.close()

        self.assertEqual(events, [{"type": "message", "text": "ok"}])
        self.assertEqual(provider.started, 1)
        self.assertEqual(provider.closed, 1)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(backend.material, ["private-token", "private-token"])
        self.assertFalse(backend.provider_was_public)
        self.assertTrue(backend.closed)

    async def test_failed_start_is_cleaned_and_fully_sanitized(self):
        secret = "provider-start-secret"

        class FailingProvider(_Provider):
            async def start(self):
                raise ValueError(secret)

        provider = FailingProvider()
        driver = ExtensionRuntimeDriver(
            _Driver(), (), credential_provider=provider
        )

        with self.assertRaisesRegex(
            CredentialProviderError, "start failed with ValueError"
        ) as caught:
            await driver.invoke(_request())

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret, rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(provider.closed, 1)

    async def test_native_adk_lifespan_owns_shared_provider(self):
        from fastapi import FastAPI

        provider = _Provider()
        native = SimpleNamespace(
            name="application",
            root_agent=SimpleNamespace(name="root", description="Root"),
        )
        application = SimpleNamespace(
            native_app=native,
            credential_provider=provider,
        )

        def create_app(*, credential_service=None, **kwargs):
            self.assertIsNotNone(credential_service)
            return FastAPI(lifespan=kwargs["lifespan"])

        with tempfile.TemporaryDirectory() as temporary, patch(
            "google.adk.cli.fast_api.get_fast_api_app",
            side_effect=create_app,
        ), patch(
            "harnest.runtime.load_agent_card",
            return_value={"name": "Root"},
        ):
            app = _create_adk_fastapi_app(
                temporary,
                application=application,
                bind_host="testserver",
                session_service=None,
            )
            async with app.router.lifespan_context(app):
                self.assertEqual(provider.started, 1)
                self.assertEqual(provider.closed, 0)

        self.assertEqual(provider.closed, 1)

    async def test_current_adk_server_uses_context_backed_credentials(self):
        provider = _Provider()
        native = SimpleNamespace(
            name="application",
            root_agent=SimpleNamespace(name="root", description="Root"),
        )
        application = SimpleNamespace(
            native_app=native,
            credential_provider=provider,
        )

        with tempfile.TemporaryDirectory() as temporary, patch(
            "harnest.runtime.load_agent_card",
            return_value={"name": "Root"},
        ):
            app = _create_adk_fastapi_app(
                temporary,
                application=application,
                bind_host="testserver",
                session_service=None,
            )
            paths = {route.path for route in app.routes}
            async with app.router.lifespan_context(app):
                self.assertEqual(provider.started, 1)

        self.assertIn("/run", paths)
        self.assertIn("/run_sse", paths)
        self.assertEqual(provider.closed, 1)


class CredentialDiscoveryTests(unittest.TestCase):
    def test_discovers_one_typed_private_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            _storage_extensions(root)
            (root / "credentials.py").write_text(
                "from harnest import Credential, CredentialProvider, lifecycle\n"
                "class Provider(CredentialProvider):\n"
                "  async def resolve(self, request): return Credential('token')\n"
                "  def __repr__(self): return 'Provider<private-token>'\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider(): return Provider()\n",
                encoding="utf-8",
            )

            discovered = discover_extensions(root, framework="adk")

        self.assertIsInstance(discovered.credential_provider, CredentialProvider)
        self.assertNotIn("private-token", repr(discovered))
        self.assertNotIn(
            "credential_provider", [item.phase for item in discovered.listeners]
        )
        self.assertEqual(
            [item.name for item in discovered.context_values], []
        )

    def test_rejects_wrong_or_duplicate_provider_factories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            _storage_extensions(root)
            first = root / "credentials.py"
            first.write_text(
                "from harnest import lifecycle\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "must return CredentialProvider"
            ):
                discover_extensions(root, framework="adk")

            first.write_text(
                "from harnest import Credential, CredentialProvider, lifecycle\n"
                "class Provider(CredentialProvider):\n"
                "  async def resolve(self, request): return Credential('token')\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider(): return Provider()\n",
                encoding="utf-8",
            )
            (root / "other_credentials.py").write_text(
                first.read_text(encoding="utf-8").replace(
                    "credential_provider():", "other_provider():"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "at most one"):
                discover_extensions(root, framework="adk")

    def test_provider_factory_failure_does_not_retain_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "extensions"
            _storage_extensions(root)
            (root / "credentials.py").write_text(
                "from harnest import lifecycle\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider():\n"
                "  raise ValueError('factory-private-token')\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "failed with ValueError"
            ) as caught:
                discover_extensions(root, framework="adk")

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("factory-private-token", rendered)
        self.assertIsNone(caught.exception.__context__)

    def test_compiler_carries_provider_into_managed_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "agent.py").write_text(
                "from harnest import Agent\n"
                "root_agent = Agent(name='root', model='unused/model')\n",
                encoding="utf-8",
            )
            (root / "instructions.md").write_text("Answer.\n", encoding="utf-8")
            extensions = root / "extensions"
            _storage_extensions(extensions)
            (extensions / "credentials.py").write_text(
                "from harnest import Credential, CredentialProvider, lifecycle\n"
                "class Provider(CredentialProvider):\n"
                "  async def resolve(self, request): return Credential('token')\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider(): return Provider()\n",
                encoding="utf-8",
            )

            application = compile_application(
                root, entrypoint="agent:root_agent", framework="adk"
            )

        self.assertIsInstance(application.credential_provider, CredentialProvider)
        self.assertIsInstance(application.native_app.plugins[0], AdkCredentialPlugin)

    def test_compiler_attaches_provider_to_advanced_adk_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "agent.py").write_text(
                "from google.adk.apps import App\n"
                "from google.adk.agents import LlmAgent\n"
                "from google.adk.plugins.base_plugin import BasePlugin\n"
                "from harnest import Agent\n"
                "root_agent = Agent.advanced(\n"
                "  App(name='root', root_agent=LlmAgent(\n"
                "    name='root', model='gemini-test'\n"
                "  ), plugins=[BasePlugin(name='existing')])\n"
                ")\n",
                encoding="utf-8",
            )
            (root / "instructions.md").write_text("Answer.\n", encoding="utf-8")
            extensions = root / "extensions"
            _storage_extensions(extensions)
            (extensions / "credentials.py").write_text(
                "from harnest import Credential, CredentialProvider, lifecycle\n"
                "class Provider(CredentialProvider):\n"
                "  async def resolve(self, request): return Credential('token')\n"
                "@lifecycle.credential_provider\n"
                "def credential_provider(): return Provider()\n",
                encoding="utf-8",
            )

            application = compile_application(
                root,
                entrypoint="agent:root_agent",
                framework="adk",
                mode="advanced",
            )

        self.assertIsInstance(application.native_app.plugins[0], AdkCredentialPlugin)
        self.assertEqual(application.native_app.plugins[1].name, "existing")


if __name__ == "__main__":
    unittest.main()
