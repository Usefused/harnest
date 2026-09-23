import asyncio
import contextvars
import traceback
import unittest

from harnest import context
from harnest.context import (
    ContextUnavailableError,
    activate_context,
    create_agent_context,
)
from harnest.credentials import (
    Credential,
    CredentialContext,
    CredentialProvider,
    CredentialProviderError,
    CredentialRequest,
    CredentialUnavailableError,
    _activate_credential_provider,
    credentials,
)
from harnest.runtime_auth import (
    AuthPrincipal,
    _activate_authenticated_principal,
    _active_authenticated_principal,
    _principal_handoff,
    _ACTIVE_HANDOFF,
)


class _RecordingProvider(CredentialProvider):
    def __init__(self, value=None):
        self.value = value if value is not None else Credential("token")
        self.requests = []

    async def resolve(self, request):
        self.requests.append(request)
        return self.value


class _FailingProvider(CredentialProvider):
    async def resolve(self, request):
        try:
            raise ValueError("nested-secret")
        except ValueError as error:
            raise RuntimeError("provider-secret") from error


def _invocation():
    return create_agent_context(
        framework="adk",
        agent_name="support",
        invocation_id="run-1",
        user_id="user-1",
        session_id="session-1",
        metadata={"private": "not-forwarded"},
        resources={"visible": "resource"},
    )


class CredentialContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_requires_context_and_private_provider(self):
        provider = _RecordingProvider()
        active = _invocation()

        with self.assertRaises(ContextUnavailableError):
            await credentials.resolve("billing")
        with activate_context(active):
            with self.assertRaisesRegex(
                CredentialUnavailableError, "no credential provider"
            ):
                await credentials.resolve("billing")
            with _activate_credential_provider(provider):
                self.assertEqual(active._resources, {"visible": "resource"})

    async def test_request_contains_identity_and_normalized_scopes(self):
        provider = _RecordingProvider(Credential({"Authorization": "secret"}))
        active = _invocation()

        with activate_context(active), _activate_credential_provider(provider):
            resolved = await credentials.resolve(
                "https://billing.example", [" read ", "write", "read"]
            )

        self.assertEqual(resolved.reveal(), {"Authorization": "secret"})
        self.assertEqual(
            provider.requests,
            [
                CredentialRequest(
                    audience="https://billing.example",
                    scopes=("read", "write"),
                    framework="adk",
                    agent_name="support",
                    invocation_id="run-1",
                    session_id="session-1",
                    principal=AuthPrincipal("user-1"),
                )
            ],
        )
        self.assertFalse(hasattr(provider.requests[0], "user_id"))
        self.assertFalse(hasattr(provider.requests[0], "metadata"))
        self.assertEqual(active._resources, {"visible": "resource"})

    async def test_request_rejects_empty_identity_and_invalid_scopes(self):
        fields = {
            "audience": "billing",
            "scopes": (),
            "framework": "adk",
            "agent_name": "support",
            "invocation_id": "run-1",
            "session_id": "session-1",
            "principal": AuthPrincipal("user-1"),
        }
        for name in (
            "audience",
            "framework",
            "agent_name",
            "invocation_id",
            "session_id",
        ):
            invalid = dict(fields, **{name: "  "})
            with self.subTest(field=name), self.assertRaisesRegex(
                ValueError, "non-empty"
            ):
                CredentialRequest(**invalid)
        with self.assertRaisesRegex(TypeError, "iterable of strings"):
            CredentialRequest(**dict(fields, scopes="read"))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            CredentialRequest(**dict(fields, scopes=("read", " ")))
        with self.assertRaisesRegex(TypeError, "must be AuthPrincipal"):
            CredentialRequest(**dict(fields, principal=object()))

    async def test_context_credentials_is_a_private_capability_alias(self):
        provider = _RecordingProvider()
        active = _invocation()

        with activate_context(active), _activate_credential_provider(provider):
            self.assertIsInstance(context.credentials, CredentialContext)
            resolved = await context.credentials.resolve("billing")

        self.assertEqual(resolved.reveal(), "token")
        self.assertNotIn("credentials", active._resources)

    async def test_principal_credentials_are_complete_private_request_context(self):
        secret = "Bearer browser-secret"
        principal = AuthPrincipal(
            "user-1",
            {"tenant_id": "tenant-1"},
            {
                "browser": Credential(secret),
                "gateway": Credential("signed-gateway-value"),
            },
        )
        provider = _RecordingProvider()

        with activate_context(_invocation()), _activate_credential_provider(
            provider
        ), _activate_authenticated_principal(principal):
            await context.credentials.resolve("threadify-engine")

        request = provider.requests[0]
        self.assertIs(request.principal, principal)
        self.assertEqual(request.principal.claims["tenant_id"], "tenant-1")
        self.assertEqual(
            request.principal.credentials["browser"].reveal(), secret
        )
        self.assertFalse(
            any(
                hasattr(request, name)
                for name in ("user_id", "headers", "cookies", "body", "metadata")
            )
        )
        self.assertNotIn(secret, repr(principal))
        self.assertNotIn(secret, repr(request))

    async def test_credentials_and_provider_errors_are_fully_redacted(self):
        secret = "raw-secret-material"
        value = Credential(secret)
        self.assertNotIn(secret, repr(value))
        self.assertNotIn(secret, str(value))
        self.assertEqual(value.reveal(), secret)

        with activate_context(_invocation()), _activate_credential_provider(
            _FailingProvider()
        ):
            with self.assertRaisesRegex(
                CredentialProviderError, "failed with RuntimeError"
            ) as caught:
                await credentials.resolve("billing")

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("provider-secret", rendered)
        self.assertNotIn("nested-secret", rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    async def test_none_and_wrong_result_types_fail_clearly(self):
        for value, error, message in (
            (None, CredentialUnavailableError, "did not return"),
            ("raw-secret", CredentialProviderError, "must return Credential"),
        ):
            provider = _RecordingProvider()
            provider.value = value
            with self.subTest(value=value), activate_context(
                _invocation()
            ), _activate_credential_provider(provider):
                with self.assertRaisesRegex(error, message) as caught:
                    await credentials.resolve("billing")
            self.assertNotIn("raw-secret", str(caught.exception))

    async def test_nested_bindings_restore_and_copied_tasks_are_revoked(self):
        outer = _RecordingProvider(Credential("outer"))
        inner = _RecordingProvider(Credential("inner"))
        release = asyncio.Event()

        async def resolve_late():
            await release.wait()
            return await credentials.resolve("billing")

        with activate_context(_invocation()), _activate_credential_provider(outer):
            self.assertEqual((await credentials.resolve("billing")).reveal(), "outer")
            with _activate_credential_provider(inner):
                self.assertEqual(
                    (await credentials.resolve("billing")).reveal(), "inner"
                )
                late = asyncio.create_task(resolve_late())
            self.assertEqual((await credentials.resolve("billing")).reveal(), "outer")
            release.set()
            with self.assertRaises(CredentialUnavailableError):
                await late


class PrincipalHandoffTests(unittest.TestCase):
    def test_refresh_uses_current_request_and_revokes_copied_contexts(self):
        """A suspended run and its children share only the current HTTP lifetime."""

        original = AuthPrincipal("alice", credentials={"browser": Credential("old")})
        fresh = AuthPrincipal("alice", credentials={"browser": Credential("fresh")})
        with _activate_authenticated_principal(original):
            owned = contextvars.copy_context()
            scope = _principal_handoff("alice")
            owned.run(scope.__enter__)
            child = owned.copy()
            handoff = owned.run(_ACTIVE_HANDOFF.get)
            self.assertIs(child.run(_active_authenticated_principal), original)
        self.assertIsNone(owned.run(_active_authenticated_principal))
        with self.assertRaises(PermissionError):
            handoff.refresh()
        with _activate_authenticated_principal(AuthPrincipal("bob")):
            with self.assertRaises(PermissionError):
                handoff.refresh()
        with _activate_authenticated_principal(fresh):
            handoff.refresh()
            self.assertIs(owned.run(_active_authenticated_principal), fresh)
            self.assertIs(child.run(_active_authenticated_principal), fresh)
            owned.run(scope.__exit__, None, None, None)
            self.assertIsNone(child.run(_active_authenticated_principal))
        self.assertIsNone(child.run(_active_authenticated_principal))

    def test_resuming_request_expiry_revokes_a_still_suspended_run(self):
        """Refreshing a handoff never upgrades request authority to task authority."""

        with _activate_authenticated_principal(AuthPrincipal("alice")):
            owned = contextvars.copy_context()
            scope = _principal_handoff("alice")
            owned.run(scope.__enter__)
            handoff = owned.run(_ACTIVE_HANDOFF.get)
        with _activate_authenticated_principal(AuthPrincipal("alice")):
            handoff.refresh()
            self.assertIsNotNone(owned.run(_active_authenticated_principal))
        self.assertIsNone(owned.run(_active_authenticated_principal))
        owned.run(scope.__exit__, None, None, None)

    def test_overlapping_requests_keep_independent_revocation_and_seal_on_close(self):
        """A shorter request cannot revoke a longer accepted request or reopen a run."""

        with _activate_authenticated_principal(AuthPrincipal("alice")):
            owned = contextvars.copy_context()
            scope = _principal_handoff("alice")
            handoff = owned.run(scope.__enter__)
        first = AuthPrincipal("alice", claims={"request": "first"})
        second = AuthPrincipal("alice", claims={"request": "second"})
        with _activate_authenticated_principal(first):
            handoff.refresh()
            with _activate_authenticated_principal(second):
                handoff.refresh()
                self.assertIs(owned.run(_active_authenticated_principal), second)
            self.assertIs(owned.run(_active_authenticated_principal), first)
            owned.run(scope.__exit__, None, None, None)
            with self.assertRaisesRegex(PermissionError, "ended"):
                handoff.refresh()
            self.assertIsNone(owned.run(_active_authenticated_principal))

    def test_anonymous_resume_cannot_downgrade_an_authenticated_run(self):
        """Authentication remains required after every accepted binding expires."""

        with _principal_handoff("alice") as handoff:
            with _activate_authenticated_principal(AuthPrincipal("alice")):
                handoff.refresh()
            with self.assertRaisesRegex(PermissionError, "requires authentication"):
                handoff.refresh()


if __name__ == "__main__":
    unittest.main()
