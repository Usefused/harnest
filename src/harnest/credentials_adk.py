"""Bind private Harnest credentials to one native ADK invocation."""

from __future__ import annotations

import warnings
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from google.adk.plugins.base_plugin import BasePlugin

_ADK_CREDENTIAL_WARNING = r"^\[EXPERIMENTAL\] BaseCredentialService:"
with warnings.catch_warnings():
    # ADK 2.8 marks this base class experimental. Suppress only its own import
    # warning if a future compatible release begins emitting one at import.
    warnings.filterwarnings(
        "ignore", message=_ADK_CREDENTIAL_WARNING, category=UserWarning
    )
    from google.adk.auth.auth_credential import AuthCredential
    from google.adk.auth.credential_service.base_credential_service import (
        BaseCredentialService,
    )

from .context import (
    AgentContext,
    activate_context,
    create_agent_context,
    revoke_context,
)
from .credentials import (
    CredentialProvider,
    _activate_credential_provider,
    credentials,
)
from .runtime_auth import _active_authenticated_principal

if TYPE_CHECKING:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.auth.auth_tool import AuthConfig


class AdkCredentialBindingError(RuntimeError):
    """Raised when ADK cannot safely establish or release a private binding."""


class AdkCredentialServiceError(RuntimeError):
    """Raised when ADK requests an unsafe credential-service operation."""


@dataclass(slots=True)
class _InvocationScope:
    """Keep revocable context state private for one ADK invocation."""

    invocation_id: str
    agent_context: AgentContext = field(repr=False)
    stack: ExitStack = field(repr=False)


class AdkCredentialPlugin(BasePlugin):
    """Expose an application provider only during native ADK execution."""

    def __init__(self, provider: CredentialProvider) -> None:
        """Configure binding without taking ownership of provider lifecycle."""

        if not isinstance(provider, CredentialProvider):
            raise TypeError("credential provider must implement CredentialProvider")
        super().__init__(name="_harnest_invocation_credentials")
        self._provider = provider
        self._scopes: dict[str, _InvocationScope] = {}
        # Native callbacks may overlap across runner tasks, but scope map
        # operations contain no awaits and therefore need only a short lock.
        self._scope_lock = Lock()

    def __repr__(self) -> str:
        """Describe the adapter without rendering its credential authority."""

        return f"{type(self).__name__}(name={self.name!r})"

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Bind private identity and credentials before ADK starts agent tasks."""

        scope, failure = _open_scope_safely(self._provider, invocation_context)
        if failure is not None:
            raise failure
        assert scope is not None
        if self._remember_scope(scope):
            return

        # A duplicate identifier must not replace a live invocation's
        # revocation handle or allow cross-invocation cleanup.
        _close_scope_safely(scope)
        raise AdkCredentialBindingError(
            "credential context is already active for this ADK invocation"
        )

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Revoke a successful invocation's private bindings exactly once."""

        await self._cleanup(invocation_context)

    async def on_run_error_callback(
        self,
        *,
        invocation_context: InvocationContext,
        error: Exception,
    ) -> None:
        """Revoke bindings after failure without inspecting the run exception."""

        del error
        await self._cleanup(invocation_context)

    async def _cleanup(self, invocation_context: InvocationContext) -> None:
        """Remove and revoke one tracked scope, tolerating repeated callbacks."""

        invocation_id, failure = _invocation_id_safely(invocation_context)
        if failure is not None:
            raise failure
        assert invocation_id is not None
        scope = self._take_scope(invocation_id)
        if scope is None:
            return
        failure = _close_scope_safely(scope)
        if failure is not None:
            raise failure

    def _remember_scope(self, scope: _InvocationScope) -> bool:
        """Atomically retain a scope unless its invocation is already active."""

        with self._scope_lock:
            if scope.invocation_id in self._scopes:
                return False
            self._scopes[scope.invocation_id] = scope
            return True

    def _take_scope(self, invocation_id: str) -> _InvocationScope | None:
        """Atomically consume a scope so cleanup remains idempotent."""

        with self._scope_lock:
            return self._scopes.pop(invocation_id, None)


class AdkContextCredentialService(BaseCredentialService):
    """Adapt invocation credentials to ADK without storing or exchanging them."""

    def __init__(self, provider: CredentialProvider) -> None:
        """Pair this boundary with an application-owned credential provider."""

        if not isinstance(provider, CredentialProvider):
            raise TypeError("credential provider must implement CredentialProvider")
        with warnings.catch_warnings():
            # BaseCredentialService warns from its wrapped initializer. Keep
            # suppression local so unrelated experimental APIs stay visible.
            warnings.filterwarnings(
                "ignore", message=_ADK_CREDENTIAL_WARNING, category=UserWarning
            )
            super().__init__()
        # Resolution still goes through the invocation ContextVar; retaining
        # the provider only makes the expected application pairing explicit.
        self._provider = provider

    def __repr__(self) -> str:
        """Describe the boundary without rendering its credential authority."""

        return f"{type(self).__name__}()"

    async def load_credential(
        self,
        auth_config: AuthConfig,
        callback_context: CallbackContext,
    ) -> AuthCredential:
        """Resolve and reveal one native credential at ADK's security boundary."""

        del callback_context
        audience, failure = _credential_audience_safely(auth_config)
        if failure is not None:
            raise failure
        assert audience is not None
        native, failure = await _resolve_adk_credential_safely(audience)
        if failure is not None:
            raise failure
        assert native is not None
        return native

    async def save_credential(
        self,
        auth_config: AuthConfig,
        callback_context: CallbackContext,
    ) -> None:
        """Refuse persistence and exchange because providers own authorization."""

        del auth_config, callback_context
        raise AdkCredentialServiceError(
            "Harnest credential providers do not persist or exchange ADK credentials"
        )


def adk_credential_plugin(provider: CredentialProvider) -> BasePlugin:
    """Create the native ADK plugin for an application credential provider."""

    return AdkCredentialPlugin(provider)


def adk_credential_service(provider: CredentialProvider) -> BaseCredentialService:
    """Create ADK's non-persisting invocation credential boundary."""

    return AdkContextCredentialService(provider)


def _credential_audience_safely(
    auth_config: AuthConfig,
) -> tuple[str | None, AdkCredentialServiceError | None]:
    """Read only ADK's credential key without retaining config failures."""

    failure_type: str | None = None
    try:
        value = getattr(auth_config, "credential_key", None)
    except Exception as error:
        failure_type = type(error).__name__
    else:
        if isinstance(value, str) and value.strip():
            return value.strip(), None
        failure_type = "ValueError"
    return None, AdkCredentialServiceError(
        f"ADK credential audience resolution failed with {failure_type}"
    )


async def _resolve_adk_credential_safely(
    audience: str,
) -> tuple[AuthCredential | None, AdkCredentialServiceError | None]:
    """Resolve native material while detaching every provider-side exception."""

    failure_type: str | None = None
    try:
        resolved = await credentials.resolve(audience)
        material = resolved.reveal()
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        return None, AdkCredentialServiceError(
            f"ADK credential resolution failed with {failure_type}"
        )
    if not isinstance(material, AuthCredential):
        return None, AdkCredentialServiceError(
            "credential provider must supply an ADK AuthCredential"
        )
    return material, None


def _open_scope_safely(
    provider: CredentialProvider, invocation_context: InvocationContext
) -> tuple[_InvocationScope | None, AdkCredentialBindingError | None]:
    """Open both bindings while detaching secret-bearing setup exceptions."""

    stack = ExitStack()
    active: AgentContext | None = None
    failure_type: str | None = None
    try:
        active = _agent_context(invocation_context)
        stack.enter_context(activate_context(active))
        stack.enter_context(_activate_credential_provider(provider))
        return _InvocationScope(active.invocation_id, active, stack), None
    except Exception as error:
        failure_type = type(error).__name__
    if active is not None:
        revoke_context(active)
    try:
        stack.close()
    except Exception as error:
        failure_type = type(error).__name__
    return None, AdkCredentialBindingError(
        f"ADK credential context setup failed with {failure_type}"
    )


def _close_scope_safely(
    scope: _InvocationScope,
) -> AdkCredentialBindingError | None:
    """Revoke copied contexts and detach any context-reset implementation error."""

    # Revocation happens before token reset so copied root/subagent tasks fail
    # closed even if ADK ever invokes cleanup from a different Context.
    revoke_context(scope.agent_context)
    failure_type: str | None = None
    try:
        scope.stack.close()
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is None:
        return None
    return AdkCredentialBindingError(
        f"ADK credential context cleanup failed with {failure_type}"
    )


def _agent_context(invocation_context: InvocationContext) -> AgentContext:
    """Translate only stable ADK identity into a private Harnest context."""

    session = getattr(invocation_context, "session", None)
    principal = _active_authenticated_principal()
    # Advanced ADK request bodies contain a caller-authored userId. Once the
    # deployment authenticates a request, only the verified principal may
    # select downstream credentials; the native value remains a local fallback.
    user_id = (
        principal.user_id
        if principal is not None
        else _required_text(getattr(session, "user_id", None), "user id")
    )
    return create_agent_context(
        framework="adk",
        agent_name=_root_agent_name(invocation_context),
        invocation_id=_required_text(
            getattr(invocation_context, "invocation_id", None), "invocation id"
        ),
        user_id=user_id,
        session_id=_required_text(getattr(session, "id", None), "session id"),
        # ADK custom metadata can propagate into events and telemetry. It is
        # intentionally excluded from the credential authorization boundary.
        metadata={},
        resources={},
    )


def _root_agent_name(invocation_context: InvocationContext) -> str:
    """Resolve ADK's root agent even when resuming from a child agent."""

    agent = getattr(invocation_context, "agent", None)
    root_agent = getattr(agent, "root_agent", None)
    if root_agent is None:
        root_agent = agent
    return _required_text(getattr(root_agent, "name", None), "root agent name")


def _invocation_id_safely(
    invocation_context: InvocationContext,
) -> tuple[str | None, AdkCredentialBindingError | None]:
    """Read cleanup identity without retaining application exception details."""

    failure_type: str | None = None
    try:
        return (
            _required_text(
                getattr(invocation_context, "invocation_id", None),
                "invocation id",
            ),
            None,
        )
    except Exception as error:
        failure_type = type(error).__name__
    return None, AdkCredentialBindingError(
        f"ADK credential context cleanup failed with {failure_type}"
    )


def _required_text(value: object, label: str) -> str:
    """Require stable non-empty ADK identity without coercing native values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ADK {label} must be a non-empty string")
    return value


__all__ = [
    "AdkCredentialBindingError",
    "AdkCredentialServiceError",
    "AdkContextCredentialService",
    "AdkCredentialPlugin",
    "adk_credential_plugin",
    "adk_credential_service",
]
