"""Framework-neutral contracts shared by transports and runtime drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

if TYPE_CHECKING:
    from .agent_principal import AgentRuntimePrincipal


RuntimeEvent = dict[str, Any]


class _ResponseEnvelope(BaseModel):
    """Fields shared by text and user-configured response request models."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    session_id: str | None = Field(default=None, alias="sessionId")
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def _non_empty_session_id(cls, value: str | None) -> str | None:
        """Keep explicitly selected sessions unambiguous."""

        if value is not None and not value.strip():
            raise ValueError("sessionId must be non-empty")
        return value


class ResponseRequest(_ResponseEnvelope):
    """Strict Pydantic body accepted by default by ``POST /responses``."""

    input: str

    @field_validator("input")
    @classmethod
    def _non_empty_input(cls, value: str) -> str:
        """Reject input that cannot produce a meaningful model turn."""

        if not value.strip():
            raise ValueError("input must be non-empty")
        return value


def response_request_model(input_schema: Any) -> type[BaseModel]:
    """Create the transport envelope around one authored Pydantic input model."""

    if input_schema is None:
        return ResponseRequest
    # A per-application model makes the user's contract visible under `input`
    # in OpenAPI without turning session and streaming controls into model data.
    return create_model(
        "ResponseRequest",
        __base__=_ResponseEnvelope,
        input=(input_schema, ...),
    )


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """Portable metadata needed by every Harnest transport."""

    id: str
    name: str
    description: str
    card: Mapping[str, Any]
    framework: str | None = None
    mode: str | None = None
    extra_endpoints: Mapping[str, str] = field(default_factory=dict)
    input_schema: Any = None
    output_schema: Any = None
    lifecycle_coverage: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A backend-independent view of a persisted agent session."""

    id: str
    user_id: str
    state: Mapping[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    application_data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """One portable transcript item with lossless framework-owned metadata."""

    id: str
    role: str
    content: Any
    created_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """The complete input passed from a transport to a runtime driver."""

    input: Any
    user_id: str
    session_id: str
    invocation_id: str
    metadata: Mapping[str, Any]
    state_delta: Mapping[str, Any]
    transport: str | None = None
    agent_principal: AgentRuntimePrincipal | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Reject public lookalikes before private runtime authority is activated."""

        from .agent_principal import validate_agent_principal

        validate_agent_principal(self.agent_principal)


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """The normalized result of a non-streaming invocation."""

    text: str
    events: Sequence[RuntimeEvent]
    result: Any
    session_id: str
    metadata: Mapping[str, Any]


@runtime_checkable
class RuntimeDriver(Protocol):
    """The only interface transports require from a framework runtime."""

    @property
    def info(self) -> AgentInfo:
        """Return immutable metadata and structured input/output contracts."""

        ...

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        """Create one identity-scoped session or report a conflict."""

        ...

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        """Resolve one session without revealing another identity's records."""

        ...

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """List a bounded identity-scoped page after an optional key."""

        ...

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        """Return an owned transcript while preserving native metadata."""

        ...

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        """Apply one state delta inside the session ownership boundary."""

        ...

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        """Delete one owned session and report whether it existed."""

        ...

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Run one complete non-streaming portable invocation."""

        ...

    def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        """Yield normalized events for one portable invocation."""

        ...

    async def close(self) -> None:
        """Release framework and application resources idempotently."""

        ...


class SessionConflictError(RuntimeError):
    """Raised by a driver when a requested session id already exists."""


class NoCustomerFacingOutputError(RuntimeError):
    """Raised when a completed invocation contains no public answer or result."""


def require_customer_facing_output(text: str, result: Any) -> None:
    """Reject reasoning-only completions without exposing their hidden content."""

    if text.strip() or result is not None:
        return
    # An empty success makes provider failures look like valid agent behavior.
    # The neutral error also keeps hidden reasoning out of every transport.
    raise NoCustomerFacingOutputError(
        "Agent completed without customer-facing output"
    )


__all__ = [
    "AgentInfo",
    "InvocationRequest",
    "InvocationResult",
    "NoCustomerFacingOutputError",
    "ResponseRequest",
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionMessage",
    "SessionRecord",
    "require_customer_facing_output",
    "response_request_model",
]
