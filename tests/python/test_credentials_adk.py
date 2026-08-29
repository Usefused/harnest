import asyncio
import traceback
import unittest
import warnings
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.openapi.models import APIKey
from fastapi.testclient import TestClient
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_service.base_credential_service import (
    BaseCredentialService,
)
from google.adk.plugins.base_plugin import BasePlugin

from harnest.context import (
    ContextUnavailableError,
    activate_context,
    context,
    create_agent_context,
    revoke_context,
)
from harnest.credentials import (
    Credential,
    CredentialProvider,
    CredentialRequest,
    credentials,
)
from harnest.credentials_adk import (
    AdkCredentialBindingError,
    AdkCredentialServiceError,
    AdkContextCredentialService,
    AdkCredentialPlugin,
    adk_credential_plugin,
    adk_credential_service,
)
from harnest.runtime_auth import (
    AuthPrincipal,
    _activate_authenticated_principal,
    install_authentication,
)


_DEFAULT_MATERIAL = object()


class _RecordingProvider(CredentialProvider):
    def __init__(self, material=_DEFAULT_MATERIAL):
        self.requests = []
        self.starts = 0
        self.closes = 0
        self.material = material

    async def start(self):
        self.starts += 1

    async def resolve(self, request):
        self.requests.append(request)
        material = (
            f"token:{request.invocation_id}"
            if self.material is _DEFAULT_MATERIAL
            else self.material
        )
        return Credential(material)

    async def close(self):
        self.closes += 1

    def __repr__(self):
        return "Provider<provider-secret>"


class _FailingProvider(CredentialProvider):
    async def resolve(self, request):
        raise RuntimeError("provider-secret")


def _auth_config(credential_key="billing"):
    return AuthConfig(
        auth_scheme=APIKey(name="X-API-Key", **{"in": "header"}),
        credential_key=credential_key,
    )


def _invocation(invocation_id="run-1", *, agent=None):
    root = SimpleNamespace(name="support")
    return SimpleNamespace(
        invocation_id=invocation_id,
        agent=agent or root,
        session=SimpleNamespace(
            user_id=f"user:{invocation_id}", id=f"session:{invocation_id}"
        ),
        _custom_metadata={"private": "must-not-propagate"},
    )


class AdkCredentialPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_context_authority_is_reused_without_early_revocation(self):
        provider = _RecordingProvider()
        plugin = adk_credential_plugin(provider)
        invocation = _invocation()
        managed = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="run-1",
            user_id="user:run-1",
            session_id="session:run-1",
            metadata={"request": "managed"},
            resources={"memory": object()},
            asset_stores={"default": object()},
            custom_stores={"users": object()},
        )

        with activate_context(managed):
            await plugin.before_run_callback(invocation_context=invocation)
            self.assertIs(context.current(), managed)
            self.assertIs(context.resource("memory"), managed._resources["memory"])
            await plugin.after_run_callback(invocation_context=invocation)
            # The outer runtime still owns this lifetime after ADK cleanup.
            self.assertIs(context.current(), managed)
            self.assertEqual(context.metadata, {"request": "managed"})

        revoke_context(managed)

    async def test_managed_context_allows_adk_native_invocation_identifier(self):
        """Keep portable correlation while using ADK's ID only for plugin cleanup."""

        provider = _RecordingProvider()
        plugin = adk_credential_plugin(provider)
        invocation = _invocation("native-run")
        invocation.session.user_id = "user:portable-run"
        invocation.session.id = "session:portable-run"
        managed = create_agent_context(
            framework="adk",
            agent_name="support",
            invocation_id="portable-run",
            user_id="user:portable-run",
            session_id="session:portable-run",
            metadata={},
            resources={},
        )

        with activate_context(managed):
            await plugin.before_run_callback(invocation_context=invocation)
            resolved = await credentials.resolve("billing")
            await plugin.after_run_callback(invocation_context=invocation)
            self.assertIs(context.current(), managed)

        revoke_context(managed)
        self.assertEqual(resolved.reveal(), "token:portable-run")
        self.assertEqual(provider.requests[0].invocation_id, "portable-run")

    async def test_resolves_only_between_callbacks_without_visible_resources(self):
        provider = _RecordingProvider()
        plugin = adk_credential_plugin(provider)
        invocation = _invocation()

        self.assertIsInstance(plugin, BasePlugin)
        self.assertNotIn("provider-secret", repr(plugin))
        await plugin.before_run_callback(invocation_context=invocation)
        active = context.current()
        self.assertEqual(active.framework, "adk")
        self.assertEqual(active.agent_name, "support")
        self.assertEqual(active.metadata, {})
        self.assertEqual(active._resources, {})
        self.assertEqual(
            (await credentials.resolve("billing", ("read",))).reveal(),
            "token:run-1",
        )

        await plugin.after_run_callback(invocation_context=invocation)
        await plugin.after_run_callback(invocation_context=invocation)
        with self.assertRaises(ContextUnavailableError):
            await credentials.resolve("billing")
        self.assertEqual(provider.starts, 0)
        self.assertEqual(provider.closes, 0)
        self.assertEqual(
            provider.requests,
            [
                CredentialRequest(
                    audience="billing",
                    scopes=("read",),
                    framework="adk",
                    agent_name="support",
                    invocation_id="run-1",
                    session_id="session:run-1",
                    principal=AuthPrincipal("user:run-1"),
                )
            ],
        )

    async def test_root_and_subagent_tasks_inherit_root_identity(self):
        provider = _RecordingProvider()
        plugin = AdkCredentialPlugin(provider)
        root = SimpleNamespace(name="coordinator")
        child = SimpleNamespace(name="specialist", root_agent=root)
        invocation = _invocation(agent=child)

        await plugin.before_run_callback(invocation_context=invocation)

        async def run_subagent():
            return await credentials.resolve("records")

        async def run_root():
            own = await credentials.resolve("billing")
            inherited = await asyncio.create_task(run_subagent())
            return own, inherited

        own, inherited = await asyncio.create_task(run_root())
        await plugin.after_run_callback(invocation_context=invocation)

        self.assertEqual(own.reveal(), "token:run-1")
        self.assertEqual(inherited.reveal(), "token:run-1")
        self.assertEqual(
            [(request.agent_name, request.audience) for request in provider.requests],
            [("coordinator", "billing"), ("coordinator", "records")],
        )

    async def test_authenticated_principal_overrides_native_request_identity(self):
        provider = _RecordingProvider()
        plugin = AdkCredentialPlugin(provider)
        invocation = _invocation("forged-native-user")

        with _activate_authenticated_principal(AuthPrincipal("verified-user")):
            await plugin.before_run_callback(invocation_context=invocation)
            await credentials.resolve("billing")
            await plugin.after_run_callback(invocation_context=invocation)

        self.assertEqual(provider.requests[0].principal.user_id, "verified-user")

    async def test_error_cleanup_revokes_copied_tasks_and_is_idempotent(self):
        provider = _RecordingProvider()
        plugin = AdkCredentialPlugin(provider)
        invocation = _invocation()
        release = asyncio.Event()

        await plugin.before_run_callback(invocation_context=invocation)

        async def resolve_late():
            await release.wait()
            return await credentials.resolve("billing")

        late = asyncio.create_task(resolve_late())
        await plugin.on_run_error_callback(
            invocation_context=invocation, error=RuntimeError("run-secret")
        )
        await plugin.on_run_error_callback(
            invocation_context=invocation, error=RuntimeError("again")
        )
        release.set()

        with self.assertRaises(ContextUnavailableError):
            await late

    async def test_overlapping_invocations_keep_independent_bindings(self):
        provider = _RecordingProvider()
        plugin = AdkCredentialPlugin(provider)
        ready = [asyncio.Event(), asyncio.Event()]
        proceed = [asyncio.Event(), asyncio.Event()]

        async def run(index):
            invocation = _invocation(f"run-{index}")
            await plugin.before_run_callback(invocation_context=invocation)
            ready[index].set()
            await proceed[index].wait()
            try:
                return (await credentials.resolve("billing")).reveal()
            finally:
                await plugin.after_run_callback(invocation_context=invocation)

        first = asyncio.create_task(run(0))
        second = asyncio.create_task(run(1))
        await asyncio.gather(*(event.wait() for event in ready))

        proceed[0].set()
        self.assertEqual(await first, "token:run-0")
        self.assertFalse(second.done())
        proceed[1].set()
        self.assertEqual(await second, "token:run-1")
        self.assertEqual(
            [request.invocation_id for request in provider.requests],
            ["run-0", "run-1"],
        )

    async def test_setup_failure_does_not_retain_secret_exception(self):
        class _ExplosiveAgent:
            @property
            def root_agent(self):
                raise RuntimeError("identity-secret")

        plugin = AdkCredentialPlugin(_RecordingProvider())
        with self.assertRaisesRegex(
            AdkCredentialBindingError, "setup failed with RuntimeError"
        ) as caught:
            await plugin.before_run_callback(
                invocation_context=_invocation(agent=_ExplosiveAgent())
            )

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("identity-secret", rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)


class AdkContextCredentialServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_returns_native_credential_and_ignores_callback_metadata(self):
        native = AuthCredential(
            auth_type=AuthCredentialTypes.API_KEY, api_key="native-secret"
        )
        provider = _RecordingProvider(native)
        plugin = AdkCredentialPlugin(provider)
        service = adk_credential_service(provider)
        invocation = _invocation()
        callback = SimpleNamespace(
            invocation_id="forged-run",
            user_id="forged-user",
            state={"credential": "callback-secret"},
            metadata={"credential": "callback-secret"},
        )

        await plugin.before_run_callback(invocation_context=invocation)
        loaded = await service.load_credential(_auth_config(" billing "), callback)
        await plugin.after_run_callback(invocation_context=invocation)

        self.assertIs(service._provider, provider)
        self.assertIs(loaded, native)
        self.assertEqual(provider.starts, 0)
        self.assertEqual(provider.closes, 0)
        self.assertEqual(provider.requests[0].audience, "billing")
        self.assertEqual(provider.requests[0].invocation_id, "run-1")
        self.assertFalse(hasattr(provider.requests[0], "metadata"))
        self.assertNotIn("provider-secret", repr(service))

    async def test_wrong_material_and_provider_failures_are_fully_redacted(self):
        for provider, message in (
            (_RecordingProvider("raw-secret"), "must supply an ADK AuthCredential"),
            (_FailingProvider(), "failed with CredentialProviderError"),
        ):
            plugin = AdkCredentialPlugin(provider)
            service = AdkContextCredentialService(provider)
            invocation = _invocation()
            await plugin.before_run_callback(invocation_context=invocation)
            with self.subTest(message=message), self.assertRaisesRegex(
                AdkCredentialServiceError, message
            ) as caught:
                await service.load_credential(
                    _auth_config(), SimpleNamespace(metadata="callback-secret")
                )
            await plugin.after_run_callback(invocation_context=invocation)

            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("raw-secret", rendered)
            self.assertNotIn("provider-secret", rendered)
            self.assertNotIn("callback-secret", rendered)
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)

    async def test_save_always_refuses_without_reading_callback_data(self):
        service = AdkContextCredentialService(_RecordingProvider())

        with self.assertRaisesRegex(
            AdkCredentialServiceError, "do not persist or exchange"
        ):
            await service.save_credential(_auth_config(), object())

    async def test_load_rejects_an_empty_audience_before_provider_resolution(self):
        provider = _RecordingProvider(
            AuthCredential(
                auth_type=AuthCredentialTypes.API_KEY, api_key="native-secret"
            )
        )
        plugin = AdkCredentialPlugin(provider)
        service = AdkContextCredentialService(provider)
        invocation = _invocation()

        await plugin.before_run_callback(invocation_context=invocation)
        with self.assertRaisesRegex(
            AdkCredentialServiceError, "audience resolution failed with ValueError"
        ):
            await service.load_credential(_auth_config("   "), object())
        await plugin.after_run_callback(invocation_context=invocation)

        self.assertEqual(provider.requests, [])

    async def test_experimental_warning_suppression_is_construction_scoped(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            service = AdkContextCredentialService(_RecordingProvider())
            warnings.warn("unrelated-warning", UserWarning)

        self.assertIsInstance(service, BaseCredentialService)
        self.assertEqual([str(item.message) for item in caught], ["unrelated-warning"])


class NativePrincipalCredentialIntegrationTests(unittest.TestCase):
    def test_authentication_selects_credentials_in_native_adk_execution(self):
        provider = _RecordingProvider()
        plugin = AdkCredentialPlugin(provider)
        app = FastAPI()

        class HeaderAuthenticator:
            async def authenticate(self, connection):
                return AuthPrincipal(
                    connection.headers["x-user"],
                    credentials={
                        "browser": Credential(connection.headers["authorization"])
                    },
                )

        @app.post("/run")
        async def run():
            invocation = _invocation("caller-controlled")
            await plugin.before_run_callback(invocation_context=invocation)
            try:
                await context.credentials.resolve("billing")
                return {"resolved": True}
            finally:
                await plugin.after_run_callback(invocation_context=invocation)

        install_authentication(app, HeaderAuthenticator())
        with TestClient(app) as client:
            response = client.post(
                "/run",
                headers={
                    "x-user": "verified-user",
                    "authorization": "Bearer browser-secret",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"resolved": True})
        self.assertEqual(provider.requests[0].principal.user_id, "verified-user")
        self.assertEqual(
            provider.requests[0].principal.credentials["browser"].reveal(),
            "Bearer browser-secret",
        )


if __name__ == "__main__":
    unittest.main()
