"""Root-owned HTTP routes that invoke the compiled agent safely."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import inspect
import re
from typing import Any

from .agent_principal import AgentRuntimePrincipal


class HTTPRouteError(ValueError):
    """An authored HTTP route violates Harnest's server contract."""


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Typed outcome returned when a custom endpoint invokes the root agent."""

    id: str
    session_id: str
    status: str
    output_text: str
    output: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    result: Any = None
    required_action: Mapping[str, Any] | None = None

    @classmethod
    def _from_payload(cls, payload: Mapping[str, Any]) -> AgentResponse:
        """Normalize the shared response coordinator's public JSON payload."""

        return cls(
            id=str(payload["id"]),
            session_id=str(payload["sessionId"]),
            status=str(payload["status"]),
            output_text=str(payload.get("outputText", "")),
            output=tuple(payload.get("output", ())),
            metadata=dict(payload.get("metadata", {})),
            result=payload.get("result"),
            required_action=payload.get("requiredAction"),
        )

    @property
    def approval_id(self) -> str | None:
        """Return the pending human-approval ID when this response requires it."""

        action = self.required_action
        if action is None or action.get("type") != "human_approval":
            return None
        value = action.get("id")
        return value if isinstance(value, str) else None

    def as_dict(self) -> dict[str, Any]:
        """Render the same JSON contract returned by Harnest's response endpoint."""

        value: dict[str, Any] = {
            "id": self.id,
            "sessionId": self.session_id,
            "status": self.status,
            "outputText": self.output_text,
            "output": list(self.output),
            "metadata": dict(self.metadata),
        }
        if self.result is not None:
            value["result"] = self.result
        if self.required_action is not None:
            value["requiredAction"] = dict(self.required_action)
        return value


_InvokeHTTPRoute = Callable[
    [Any, Any, str | None, Mapping[str, Any], AgentRuntimePrincipal | None],
    Awaitable[Mapping[str, Any]],
]


@dataclass(slots=True)
class AgentInvoker:
    """Server-bound capability for invoking the compiled root agent."""

    _invoke: _InvokeHTTPRoute | None = field(default=None, init=False, repr=False)

    async def invoke(
        self,
        *,
        connection: Any,
        input: Any,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        agent_principal: AgentRuntimePrincipal | None = None,
    ) -> AgentResponse:
        """Invoke with an optional application-authorized runtime principal."""

        if self._invoke is None:
            raise RuntimeError("AgentInvoker is available only while serving Harnest")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise ValueError("session_id must be a non-empty string")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        payload = await self._invoke(
            connection, input, session_id, dict(metadata or {}), agent_principal
        )
        return AgentResponse._from_payload(payload)

    def _bind(self, callback: _InvokeHTTPRoute) -> None:
        """Bind exactly one server-owned coordinator after driver composition."""

        if self._invoke is not None:
            raise HTTPRouteError("AgentInvoker cannot be bound to multiple servers")
        self._invoke = callback


@dataclass(frozen=True, slots=True)
class HTTPRouteExtension:
    """A validated authored router and its deferred invocation capability."""

    router: Any = field(repr=False)
    invoker: AgentInvoker = field(repr=False)
    identity: str


_RESERVED_PREFIXES = (
    "/",
    "/_harnest",
    "/.well-known",
    "/agent",
    "/apps",
    "/approvals",
    "/client-tools",
    "/docs",
    "/health",
    "/healthz",
    "/live",
    "/list-apps",
    "/openapi.json",
    "/redoc",
    "/responses",
    "/run",
    "/run_live",
    "/run_sse",
    "/sessions",
    "/version",
)
_PATH_PARAMETER = re.compile(r"\{[^}/]+\}")


def create_http_route_extension(callback: Any, *, identity: str) -> HTTPRouteExtension:
    """Materialize one route factory once so compilation can reject conflicts."""

    invoker = AgentInvoker()
    failure: HTTPRouteError | None = None
    try:
        router = callback(invoker)
    except Exception as error:
        failure = HTTPRouteError(
            f"HTTP route factory {identity} failed with {type(error).__name__}"
        )
    if failure is not None:
        # Raise after leaving authored code so secret-bearing exceptions are not
        # retained through the public compiler error chain.
        raise failure
    if inspect.isawaitable(router):
        closer = getattr(router, "close", None)
        if callable(closer):
            closer()
        raise HTTPRouteError(
            f"HTTP route factory {identity} must be synchronous"
        )
    _require_api_router(router, identity=identity)
    extension = HTTPRouteExtension(router, invoker, identity)
    validate_http_route_extensions((extension,))
    return extension


def validate_http_route_extensions(
    extensions: Sequence[HTTPRouteExtension],
) -> None:
    """Reject reserved paths and duplicate authored method/path contracts."""

    seen: list[tuple[str, str, str]] = []
    for extension in extensions:
        for contract in _route_contracts(extension.router, identity=extension.identity):
            method, path = contract
            _reject_reserved_path(path, identity=extension.identity)
            previous = _route_conflict(seen, method=method, path=path)
            if previous is not None:
                raise HTTPRouteError(
                    f"HTTP route {method} {path} from {extension.identity} "
                    f"conflicts with {previous}"
                )
            seen.append((method, path, extension.identity))


def mount_http_route_extensions(
    router: Any,
    extensions: Sequence[HTTPRouteExtension],
    invoke: _InvokeHTTPRoute,
) -> None:
    """Bind and mount compiled routers after the final runtime driver exists."""

    for extension in extensions:
        extension.invoker._bind(invoke)
        router.include_router(extension.router)


def _require_api_router(router: Any, *, identity: str) -> None:
    """Keep authored route composition on FastAPI's supported router surface."""

    from fastapi import APIRouter

    if not isinstance(router, APIRouter):
        raise HTTPRouteError(
            f"HTTP route factory {identity} must return fastapi.APIRouter; "
            f"got {type(router).__name__}"
        )


def _route_contracts(
    router: Any,
    *,
    identity: str,
    prefix: str = "",
    ancestors: frozenset[int] = frozenset(),
) -> tuple[tuple[str, str], ...]:
    """Inspect HTTP leaves with their effective paths across FastAPI versions."""

    from fastapi import routing

    if id(router) in ancestors:
        raise HTTPRouteError(f"HTTP route factory {identity} contains a router cycle")
    ancestors = ancestors | {id(router)}
    # FastAPI 0.137+ retains included routers instead of cloning their leaves.
    # Recognize that concrete wrapper only; arbitrary mounts remain forbidden.
    included_type = getattr(routing, "_IncludedRouter", ())

    contracts: list[tuple[str, str]] = []
    for route in router.routes:
        if isinstance(route, routing.APIRoute):
            contracts.extend(
                (method, prefix + route.path) for method in sorted(route.methods)
            )
            continue
        if isinstance(route, included_type):
            # The leaf already includes its own router prefix; each include
            # context supplies its parent/include prefix. Keep every occurrence
            # so mounting one child twice still participates in conflict checks.
            contracts.extend(_route_contracts(
                route.original_router,
                identity=identity,
                prefix=prefix + route.include_context.prefix,
                ancestors=ancestors,
            ))
            continue
        raise HTTPRouteError(
            f"HTTP route factory {identity} may contain only FastAPI HTTP routes; "
            f"got {type(route).__name__}"
        )
    return tuple(contracts)


def _reject_reserved_path(path: str, *, identity: str) -> None:
    """Keep Harnest, playground, and advanced ADK namespaces authoritative."""

    if not isinstance(path, str) or not path.startswith("/"):
        raise HTTPRouteError(f"HTTP route from {identity} must use an absolute path")
    if ":path}" in path:
        # Catch-all converters make ownership depend on route order and can
        # silently swallow both present and future application endpoints.
        raise HTTPRouteError(
            f"HTTP route {path} from {identity} cannot use a catch-all path converter"
        )
    for prefix in _RESERVED_PREFIXES:
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            raise HTTPRouteError(
                f"HTTP route {path} from {identity} uses reserved Harnest path {prefix}"
            )


def _normalize_path(path: str) -> str:
    """Treat differently named path parameters as the same route contract."""

    return _PATH_PARAMETER.sub("{}", path.rstrip("/") or "/")


def _route_conflict(
    seen: Sequence[tuple[str, str, str]], *, method: str, path: str
) -> str | None:
    """Find a same-method route whose literals or parameters can overlap."""

    for previous_method, previous_path, identity in seen:
        if previous_method == method and _paths_overlap(previous_path, path):
            return identity
    return None


def _paths_overlap(first: str, second: str) -> bool:
    """Conservatively compare fixed-length FastAPI route templates."""

    first_parts = _normalize_path(first).strip("/").split("/")
    second_parts = _normalize_path(second).strip("/").split("/")
    if len(first_parts) != len(second_parts):
        return False
    return all(
        left == right or left == "{}" or right == "{}"
        for left, right in zip(first_parts, second_parts)
    )


__all__ = ["AgentInvoker", "AgentResponse", "HTTPRouteError"]
