"""Lazy outbound A2A 1.x client and portable remote-agent graph node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import re
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from google.protobuf import json_format

from a2a import helpers
from a2a.client import Client, ClientCallContext, ClientConfig, create_client
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskState,
)
from a2a.utils.constants import TransportProtocol

from .credentials import Credential, CredentialUnavailableError, credentials
from .graph import Event, GraphContext


MAX_AGENT_CARD_BYTES = 512 * 1024
_REMOTE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TERMINAL_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)


class A2AClientError(RuntimeError):
    """An outbound A2A interaction failed Harnest policy or protocol handling."""


class RemoteAgentError(A2AClientError):
    """A remote agent reached a terminal failure state."""


@dataclass(frozen=True, slots=True)
class A2AResult:
    """Minimum portable projection of a direct Message or tracked A2A Task."""

    text: str
    data: Any
    context_id: str | None
    task_id: str | None
    state: str
    message: Message | None = field(default=None, repr=False, compare=False)
    task: Task | None = field(default=None, repr=False, compare=False)

    @property
    def terminal(self) -> bool:
        """Return whether the remote interaction no longer needs observation."""

        return self.state in {"completed", "failed", "canceled", "rejected"}

    def as_dict(self) -> dict[str, Any]:
        """Serialize only portable output and opaque task references."""

        return {
            "text": self.text,
            "data": self.data,
            "contextId": self.context_id,
            "taskId": self.task_id,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class A2AUpdate:
    """One normalized outbound stream update without protobuf coupling."""

    kind: str
    task_id: str | None
    context_id: str | None
    state: str | None = None
    text: str = ""
    data: Any = None


@dataclass(slots=True)
class _ResponseAccumulator:
    """Collect task updates while retaining the latest cancelable identity."""

    task: Task | None = None
    message: Message | None = None
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    text: list[str] = field(default_factory=list)
    data: list[Any] = field(default_factory=list)

    def apply(self, response: StreamResponse) -> A2AUpdate:
        """Update the local projection from one discriminated protocol event."""

        kind = response.WhichOneof("payload")
        if kind == "message":
            self.message = Message()
            self.message.CopyFrom(response.message)
            self.state = TaskState.TASK_STATE_COMPLETED
            self._parts(response.message.parts)
        elif kind == "task":
            self.task = Task()
            self.task.CopyFrom(response.task)
            self.state = response.task.status.state
            # A Task is an authoritative snapshot. Reset accumulated content so
            # explicit polling cannot duplicate artifacts seen in an earlier snapshot.
            self.text.clear()
            self.data.clear()
            for artifact in response.task.artifacts:
                self._parts(artifact.parts)
            if response.task.status.HasField("message"):
                self._parts(response.task.status.message.parts)
        elif kind == "status_update":
            self.state = response.status_update.status.state
            if response.status_update.status.HasField("message"):
                self._parts(response.status_update.status.message.parts)
        elif kind == "artifact_update":
            self._parts(response.artifact_update.artifact.parts)
        else:
            raise A2AClientError("Remote agent returned an empty stream response")
        return self._update(kind)

    def result(self) -> A2AResult:
        """Build the final or interrupted response after stream consumption."""

        context_id, task_id = self._identifiers()
        data: Any = None
        if len(self.data) == 1:
            data = self.data[0]
        elif self.data:
            data = list(self.data)
        return A2AResult(
            text="".join(self.text),
            data=data,
            context_id=context_id,
            task_id=task_id,
            state=_state_name(self.state),
            message=self.message,
            task=self.task,
        )

    def _parts(self, parts: Sequence[Any]) -> None:
        """Collect only text and structured data; raw and URL parts stay referenced."""

        self.text.extend(helpers.get_text_parts(parts))
        self.data.extend(helpers.get_data_parts(parts))

    def _identifiers(self) -> tuple[str | None, str | None]:
        """Prefer task identity while preserving direct-message contexts."""

        if self.task is not None:
            return self.task.context_id or None, self.task.id or None
        if self.message is not None:
            return self.message.context_id or None, self.message.task_id or None
        return None, None

    def _update(self, kind: str) -> A2AUpdate:
        """Project the latest event without repeating accumulated content."""

        context_id, task_id = self._identifiers()
        text = self.text[-1] if self.text and kind in {"message", "status_update", "artifact_update"} else ""
        data = self.data[-1] if self.data and kind in {"message", "status_update", "artifact_update"} else None
        return A2AUpdate(
            kind=kind,
            task_id=task_id,
            context_id=context_id,
            state=_state_name(self.state),
            text=text,
            data=data,
        )


class A2AClient:
    """Discover and invoke one remote agent without background polling or listing."""

    def __init__(
        self,
        card_url: str,
        *,
        card: Mapping[str, Any] | None = None,
        timeout: float = 300,
        poll_interval: float = 0.5,
        streaming: bool = False,
        allowed_hosts: Sequence[str] = (),
        allow_insecure: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Validate explicit network authority and defer all I/O until first use."""

        _validate_timeout(timeout)
        _validate_poll_interval(poll_interval)
        self._card_url = _validated_url(
            card_url,
            allow_insecure=allow_insecure,
            allowed_hosts=allowed_hosts,
        )
        self._card_value = None if card is None else dict(card)
        self._timeout = float(timeout)
        self._poll_interval = float(poll_interval)
        self._streaming = bool(streaming)
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._allow_insecure = allow_insecure
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        )
        self._client: Client | None = None
        self._card: AgentCard | None = None
        self._connect_lock = asyncio.Lock()
        self._closed = False

    @property
    def card(self) -> AgentCard | None:
        """Return the validated cached card without triggering network I/O."""

        return self._card

    async def connect(self) -> AgentCard:
        """Resolve and validate the Agent Card exactly once for this client."""

        if self._closed:
            raise A2AClientError("A2A client is closed")
        if self._client is not None and self._card is not None:
            return self._card
        async with self._connect_lock:
            if self._client is not None and self._card is not None:
                return self._card
            card = await self._resolve_card()
            self._validate_interfaces(card)
            config = ClientConfig(
                streaming=self._streaming,
                polling=False,
                httpx_client=self._http,
                supported_protocol_bindings=[
                    TransportProtocol.JSONRPC,
                    TransportProtocol.HTTP_JSON,
                ],
            )
            try:
                client = await create_client(card, client_config=config)
            except Exception as exc:
                raise A2AClientError("Agent Card has no supported interface") from exc
            self._card = card
            self._client = client
            return card

    async def send(
        self,
        value: Any,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        accepted_output_modes: Sequence[str] = (),
        return_immediately: bool = False,
        wait: bool = True,
        credential: Credential | None = None,
    ) -> A2AResult:
        """Send one message and poll only when the caller requested a final result."""

        client = await self._require_client()
        request = _send_request(
            value,
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
            accepted_output_modes=accepted_output_modes,
            return_immediately=return_immediately,
        )
        call_context = _call_context(self._timeout, credential)
        accumulator = _ResponseAccumulator()
        deadline = asyncio.get_running_loop().time() + self._timeout
        try:
            await asyncio.wait_for(
                self._consume_send(client, request, call_context, accumulator),
                timeout=_remaining(deadline),
            )
            if wait and _needs_poll(accumulator):
                await self._poll_until_terminal(
                    client, accumulator, call_context, deadline
                )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            await self._cancel_if_active(
                client,
                accumulator,
                _bounded_call_context(call_context, min(5, self._timeout)),
            )
            raise
        return accumulator.result()

    async def stream(
        self,
        value: Any,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        credential: Credential | None = None,
    ) -> AsyncIterator[A2AUpdate]:
        """Yield remote updates without issuing any implicit follow-up operations."""

        if not self._streaming:
            raise A2AClientError(
                "Streaming requires A2AClient(streaming=True)"
            )
        client = await self._require_client()
        request = _send_request(
            value,
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
            accepted_output_modes=(),
            return_immediately=False,
        )
        context = _call_context(self._timeout, credential)
        accumulator = _ResponseAccumulator()
        try:
            async for response in client.send_message(request, context=context):
                yield accumulator.apply(response)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            await self._cancel_if_active(
                client,
                accumulator,
                _bounded_call_context(context, min(5, self._timeout)),
            )
            raise

    async def get_task(
        self,
        task_id: str,
        *,
        history_length: int = 0,
        credential: Credential | None = None,
    ) -> Task:
        """Fetch one task only after an explicit caller request."""

        client = await self._require_client()
        request = GetTaskRequest(id=_required_text(task_id, "task_id"))
        request.history_length = _history_length(history_length)
        return await client.get_task(
            request, context=_call_context(self._timeout, credential)
        )

    async def list_tasks(
        self,
        *,
        page_size: int = 50,
        page_token: str = "",
        context_id: str = "",
        include_artifacts: bool = False,
        credential: Credential | None = None,
    ) -> ListTasksResponse:
        """List a bounded page only when directly requested by application code."""

        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        client = await self._require_client()
        request = ListTasksRequest(
            page_size=page_size,
            page_token=page_token,
            context_id=context_id,
            include_artifacts=include_artifacts,
            history_length=0,
        )
        return await client.list_tasks(
            request, context=_call_context(self._timeout, credential)
        )

    async def cancel_task(
        self, task_id: str, *, credential: Credential | None = None
    ) -> Task:
        """Cancel one task only in response to explicit local cancellation."""

        client = await self._require_client()
        return await client.cancel_task(
            CancelTaskRequest(id=_required_text(task_id, "task_id")),
            context=_call_context(self._timeout, credential),
        )

    async def subscribe(
        self, task_id: str, *, credential: Credential | None = None
    ) -> AsyncIterator[A2AUpdate]:
        """Subscribe to an existing task without fetching or listing others."""

        client = await self._require_client()
        accumulator = _ResponseAccumulator()
        stream = client.subscribe(
            SubscribeToTaskRequest(id=_required_text(task_id, "task_id")),
            context=_call_context(self._timeout, credential),
        )
        async for response in stream:
            yield accumulator.apply(response)

    async def close(self) -> None:
        """Close the selected transport or the unused discovery client once."""

        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.close()
        else:
            await self._http.aclose()

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _require_client(self) -> Client:
        """Return the lazy transport after card discovery and interface policy."""

        await self.connect()
        if self._client is None:  # pragma: no cover - connect invariant
            raise A2AClientError("A2A client failed to initialize")
        return self._client

    async def _resolve_card(self) -> AgentCard:
        """Load a pinned mapping or one bounded public discovery document."""

        if self._card_value is not None:
            value = self._card_value
        else:
            try:
                response = await self._http.get(self._card_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise A2AClientError("Unable to retrieve remote Agent Card") from exc
            if len(response.content) > MAX_AGENT_CARD_BYTES:
                raise A2AClientError("Remote Agent Card exceeds the size limit")
            try:
                value = response.json()
            except (UnicodeDecodeError, ValueError) as exc:
                raise A2AClientError("Remote Agent Card is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise A2AClientError("Remote Agent Card must be a JSON object")
        try:
            return json_format.ParseDict(dict(value), AgentCard())
        except (TypeError, ValueError) as exc:
            raise A2AClientError("Remote Agent Card does not match A2A 1.x") from exc

    def _validate_interfaces(self, card: AgentCard) -> None:
        """Prevent a discovered card from redirecting authority to another host."""

        card_host = urlsplit(self._card_url).hostname or ""
        allowed = set(self._allowed_hosts) | {card_host.lower()}
        if not card.supported_interfaces:
            raise A2AClientError("Remote Agent Card declares no interfaces")
        for interface in card.supported_interfaces:
            try:
                _validated_url(
                    interface.url,
                    allow_insecure=self._allow_insecure,
                    allowed_hosts=allowed,
                )
            except ValueError as exc:
                raise A2AClientError(
                    "Remote Agent Card interface violates network policy"
                ) from exc

    async def _consume_send(
        self,
        client: Client,
        request: SendMessageRequest,
        context: ClientCallContext,
        accumulator: _ResponseAccumulator,
    ) -> None:
        """Consume the selected transport without inventing extra client calls."""

        async for response in client.send_message(request, context=context):
            accumulator.apply(response)

    async def _poll_until_terminal(
        self,
        client: Client,
        accumulator: _ResponseAccumulator,
        context: ClientCallContext,
        deadline: float,
    ) -> None:
        """Poll only a non-terminal Task returned from an explicit waiting send."""

        while _needs_poll(accumulator):
            remaining = _remaining(deadline)
            await asyncio.sleep(min(self._poll_interval, remaining))
            task = await client.get_task(
                GetTaskRequest(id=accumulator.task.id, history_length=0),
                context=_bounded_call_context(context, _remaining(deadline)),
            )
            update = StreamResponse()
            update.task.CopyFrom(task)
            accumulator.apply(update)

    async def _cancel_if_active(
        self,
        client: Client,
        accumulator: _ResponseAccumulator,
        context: ClientCallContext,
    ) -> None:
        """Best-effort remote cancellation without masking the local exception."""

        if accumulator.task is None or accumulator.state in _TERMINAL_STATES:
            return
        try:
            await client.cancel_task(
                CancelTaskRequest(id=accumulator.task.id), context=context
            )
        except Exception:
            return


@dataclass(frozen=True, slots=True)
class RemoteAgent:
    """A first-class remote A2A participant usable as a portable graph node."""

    name: str
    card_url: str
    description: str = ""
    audience: str | None = None
    scopes: tuple[str, ...] = ()
    timeout: float = 300
    allowed_hosts: tuple[str, ...] = ()
    allow_insecure: bool = False
    card: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Freeze remote policy so compilation cannot retain mutable card data."""

        if not isinstance(self.name, str) or not _REMOTE_NAME.fullmatch(self.name):
            raise ValueError("remote agent name must be a valid Python identifier")
        if not isinstance(self.description, str):
            raise TypeError("remote agent description must be a string")
        _validate_timeout(self.timeout)
        if self.audience is not None:
            _required_text(self.audience, "audience")
        if any(not isinstance(scope, str) or not scope.strip() for scope in self.scopes):
            raise ValueError("remote agent scopes must be non-empty strings")
        _validated_url(
            self.card_url,
            allow_insecure=self.allow_insecure,
            allowed_hosts=self.allowed_hosts,
        )
        if self.card is not None:
            object.__setattr__(self, "card", MappingProxyType(dict(self.card)))

    async def __call__(self, value: Any, *, context: GraphContext) -> Event:
        """Delegate one graph value and retain only opaque conversation references."""

        state_key = f"a2a.{self.name}"
        remote_state = context.state.get(state_key, {})
        context_id = remote_state.get("contextId") if isinstance(remote_state, Mapping) else None
        task_id = remote_state.get("taskId") if isinstance(remote_state, Mapping) else None
        credential = await self._credential()
        async with A2AClient(
            self.card_url,
            card=self.card,
            timeout=self.timeout,
            allowed_hosts=self.allowed_hosts,
            allow_insecure=self.allow_insecure,
        ) as client:
            result = await client.send(
                value,
                context_id=context_id,
                task_id=task_id,
                credential=credential,
            )
        if result.state in {"failed", "canceled", "rejected"}:
            raise RemoteAgentError(
                f"remote agent {self.name!r} finished with state {result.state}"
            )
        next_state = {"contextId": result.context_id}
        if not result.terminal and result.task_id:
            next_state["taskId"] = result.task_id
        output = result.data if result.data is not None else result.text
        return Event(
            output=output,
            message=result.text or None,
            state_delta={state_key: next_state},
        )

    def as_tool(self) -> Any:
        """Expose delegation as an explicit text tool when agent semantics are unsuitable."""

        async def invoke_remote_agent(input: str) -> dict[str, Any]:
            """Send one independent request to the configured remote agent."""

            credential = await self._credential()
            async with A2AClient(
                self.card_url,
                card=self.card,
                timeout=self.timeout,
                allowed_hosts=self.allowed_hosts,
                allow_insecure=self.allow_insecure,
            ) as client:
                return (await client.send(input, credential=credential)).as_dict()

        invoke_remote_agent.__name__ = self.name
        invoke_remote_agent.__doc__ = self.description or (
            f"Delegate work to the {self.name} remote agent."
        )
        return invoke_remote_agent

    async def _credential(self) -> Credential | None:
        """Resolve outbound authority only inside an active Harnest invocation."""

        if self.audience is None:
            return None
        try:
            return await credentials.resolve(self.audience, self.scopes)
        except CredentialUnavailableError as exc:
            raise A2AClientError("Remote agent credential is unavailable") from exc


def _send_request(
    value: Any,
    *,
    context_id: str | None,
    task_id: str | None,
    metadata: Mapping[str, Any] | None,
    accepted_output_modes: Sequence[str],
    return_immediately: bool,
) -> SendMessageRequest:
    """Build one official request without assigning a server-owned task ID."""

    message = (
        helpers.new_text_message(value, role=Role.ROLE_USER)
        if isinstance(value, str)
        else helpers.new_data_message(value, role=Role.ROLE_USER)
    )
    if context_id is not None:
        message.context_id = _required_text(context_id, "context_id")
    if task_id is not None:
        message.task_id = _required_text(task_id, "task_id")
    configuration = SendMessageConfiguration(
        accepted_output_modes=list(accepted_output_modes),
        return_immediately=return_immediately,
        history_length=0,
    )
    return SendMessageRequest(
        message=message,
        configuration=configuration,
        metadata=dict(metadata or {}),
    )


def _call_context(timeout: float, credential: Credential | None) -> ClientCallContext:
    """Reveal credentials only into the final outbound service-parameter map."""

    return ClientCallContext(
        timeout=timeout,
        service_parameters=_credential_headers(credential),
    )


def _bounded_call_context(
    context: ClientCallContext, timeout: float
) -> ClientCallContext:
    """Preserve service authority while bounding one poll by the total deadline."""

    return ClientCallContext(
        state=context.state,
        timeout=timeout,
        service_parameters=context.service_parameters,
    )


def _remaining(deadline: float) -> float:
    """Return positive time left under one outbound operation deadline."""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return remaining


def _credential_headers(credential: Credential | None) -> dict[str, str] | None:
    """Accept bearer tokens or an explicit immutable header mapping."""

    if credential is None:
        return None
    material = credential.reveal()
    if isinstance(material, str) and material:
        return {"Authorization": f"Bearer {material}"}
    if isinstance(material, Mapping):
        headers = {str(key): str(value) for key, value in material.items()}
        if headers and all(_valid_header(key, value) for key, value in headers.items()):
            return headers
    raise A2AClientError("A2A credentials must contain a token or header mapping")


def _valid_header(name: str, value: str) -> bool:
    """Reject empty or control-bearing credential headers before HTTP encoding."""

    return bool(name.strip()) and not any(char in name + value for char in "\r\n\0")


def _validated_url(
    value: str,
    *,
    allow_insecure: bool,
    allowed_hosts: Sequence[str],
) -> str:
    """Allow HTTPS generally and plaintext HTTP only under explicit local policy."""

    parsed = _parsed_a2a_url(value)
    host = parsed.hostname or ""
    _validate_allowed_host(host, allowed_hosts)
    _validate_url_security(parsed.scheme, host, allow_insecure)
    return value


def _parsed_a2a_url(value: str) -> Any:
    """Reject ambiguous URL forms before applying network authority policy."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("A2A URL must be a non-empty string")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if not host:
        raise ValueError("A2A URL must include a host")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("A2A URL must not contain credentials or fragments")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("A2A URL must use HTTP or HTTPS")
    return parsed


def _validate_allowed_host(host: str, allowed_hosts: Sequence[str]) -> None:
    """Apply an allowlist only when the caller configured one explicitly."""

    allowed = {item.lower() for item in allowed_hosts}
    if allowed and host.lower() not in allowed:
        raise ValueError("A2A URL host is outside the configured allowlist")


def _validate_url_security(scheme: str, host: str, allow_insecure: bool) -> None:
    """Reserve plaintext transport for explicit policy or local development."""

    if scheme == "http" and not allow_insecure and not _loopback_host(host):
        raise ValueError("A2A HTTP URLs require allow_insecure=True outside loopback")


def _loopback_host(host: str) -> bool:
    """Recognize local development names without resolving untrusted DNS."""

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _needs_poll(accumulator: _ResponseAccumulator) -> bool:
    """Poll only a returned Task whose latest state is not terminal or interrupted."""

    return accumulator.task is not None and accumulator.state not in (
        _TERMINAL_STATES
        | {
            TaskState.TASK_STATE_INPUT_REQUIRED,
            TaskState.TASK_STATE_AUTH_REQUIRED,
        }
    )


def _state_name(state: int) -> str:
    """Translate protocol enum names into compact portable status values."""

    names = {
        TaskState.TASK_STATE_UNSPECIFIED: "unspecified",
        TaskState.TASK_STATE_SUBMITTED: "submitted",
        TaskState.TASK_STATE_WORKING: "working",
        TaskState.TASK_STATE_COMPLETED: "completed",
        TaskState.TASK_STATE_FAILED: "failed",
        TaskState.TASK_STATE_CANCELED: "canceled",
        TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
        TaskState.TASK_STATE_REJECTED: "rejected",
        TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
    }
    return names.get(state, "unspecified")


def _history_length(value: int) -> int:
    """Keep task history opt-in and bounded."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("history_length must be between 0 and 100")
    return value


def _required_text(value: str, name: str) -> str:
    """Validate an opaque remote identifier without rewriting it."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_timeout(value: float) -> None:
    """Reject boolean, non-numeric, and non-positive request deadlines."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("A2A timeout must be greater than zero")


def _validate_poll_interval(value: float) -> None:
    """Bound active polling so configuration cannot create a hot loop."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0.05:
        raise ValueError("A2A poll_interval must be at least 0.05 seconds")


__all__ = [
    "A2AClient",
    "A2AClientError",
    "A2AResult",
    "A2AUpdate",
    "RemoteAgent",
    "RemoteAgentError",
]
