"""Borrow explicitly configured model transports without transferring ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


_TRANSPORT_ARGUMENTS = frozenset(
    {
        "client", "api_base", "api_key", "api_version", "organization",
        "extra_headers", "default_headers", "http_client",
        "headers", "ssl_verify", "custom_llm_provider",
        "azure_ad_token", "azure_ad_token_provider",
        "tenant_id", "client_id", "client_secret", "azure_username",
        "azure_password", "azure_scope",
        "aws_region_name", "aws_access_key_id", "aws_secret_access_key",
        "aws_session_token", "aws_session_name", "aws_profile_name",
        "aws_role_name", "aws_web_identity_token", "aws_sts_endpoint",
        "aws_external_id", "aws_bedrock_runtime_endpoint",
        "vertex_credentials", "vertex_project", "vertex_location",
        "vertex_ai_credentials", "vertex_ai_project", "vertex_ai_location",
    }
)
_BINDINGS_ATTRIBUTE = "__harnest_model_transport_bindings__"


class FrameworkToolClient:
    """Keep framework-executed functions out of LiteLLM's optional remote MCP gateway."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def acompletion(self, *args: Any, **kwargs: Any) -> Any:
        """Route the final async request after framework tools and hooks are applied."""
        _check_model_endpoint(kwargs)
        _route_function_tools(args, kwargs)
        try:
            return await self._delegate.acompletion(*args, **kwargs)
        except Exception as exc:
            _reraise_connection_error(exc, kwargs)
            raise

    def completion(self, *args: Any, **kwargs: Any) -> Any:
        """Apply the same tool ownership rule to synchronous and streaming calls."""
        _check_model_endpoint(kwargs)
        _route_function_tools(args, kwargs)
        try:
            return self._delegate.completion(*args, **kwargs)
        except Exception as exc:
            _reraise_connection_error(exc, kwargs)
            raise


# The placeholder endpoint `harnest init` writes; equal to the Go constant
# agentscaffold.ExampleBaseURL (test_model_transport keeps the two in step).
SCAFFOLD_BASE_URL = "https://models.example.invalid/v1"


def _check_model_endpoint(arguments: Mapping[str, Any]) -> None:
    """Catch the scaffold endpoint at invocation so offline compilation still works."""
    if arguments.get("api_base") == SCAFFOLD_BASE_URL:
        raise ValueError(
            "The example model endpoint cannot serve requests. Set OPENAI_BASE_URL "
            "to your OpenAI-compatible API and OPENAI_MODEL to a model it serves."
        )


_CONNECTION_ERROR_NAMES = frozenset(
    {"APIConnectionError", "ConnectError", "ClientConnectorError", "ConnectTimeout"}
)


def _connection_cause(error: BaseException) -> BaseException | None:
    """Return the first genuine connection error in the chain, or None."""

    current: BaseException | None = error
    seen: set[int] = set()
    # Walk explicit and implicit causes so wrapped provider exceptions (such as
    # LiteLLM InternalServerError wrapping openai.APIConnectionError) are detected.
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionError) or type(current).__name__ in _CONNECTION_ERROR_NAMES:
            return current
        current = current.__cause__ or current.__context__
    return None


def is_connection_error(error: BaseException) -> bool:
    """Report whether an exception or its cause chain represents a network connection failure."""

    return _connection_cause(error) is not None


def endpoint_origin(api_base: Any) -> str:
    """Extract only scheme and host[:port] so diagnostics never disclose credentials or paths."""

    if not isinstance(api_base, str) or not api_base.strip():
        return ""
    parsed = urlsplit(api_base.strip())
    # Exclude userinfo, path, and query so embedded tokens or routes cannot leak.
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{host}"


def _reraise_connection_error(error: Exception, kwargs: Mapping[str, Any]) -> None:
    """Convert LiteLLM's misclassified InternalServerError on connection failures to APIConnectionError."""

    # Only reclassify provider wrappers that mislabel a client-side connect error as HTTP 500.
    cause = _connection_cause(error)
    if type(error).__name__ != "InternalServerError" or cause is None:
        return
    origin = endpoint_origin(kwargs.get("api_base"))
    location = f" ({origin})" if origin else ""
    message = (
        f"Could not connect to the configured model server{location}. "
        "Check that the server is running and reachable at OPENAI_BASE_URL."
    )
    try:
        from litellm.exceptions import APIConnectionError
    except ImportError:
        raise ConnectionError(message) from cause
    # Chain from the underlying connection error so tracebacks drop LiteLLM's bogus InternalServerError.
    raise APIConnectionError(
        message=message,
        llm_provider=getattr(error, "llm_provider", "openai"),
        model=getattr(error, "model", str(kwargs.get("model", ""))),
    ) from cause


def _route_function_tools(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Bypass LiteLLM's MCP import only for a list of ordinary function schemas."""
    tools = kwargs.get("tools", args[2] if len(args) > 2 else None)
    if isinstance(tools, (list, tuple)) and tools and all(
        isinstance(tool, Mapping) and tool.get("type") == "function" for tool in tools
    ):
        # ADK and LangGraph execute these functions, including Harnest MCP tools.
        # Let LiteLLM handle mixed or provider-native tool lists as usual.
        kwargs.setdefault("_skip_mcp_handler", True)


def framework_adk_client(client_type: type[Any]) -> Any:
    """Keep ADK's native client interface while routing framework-owned tool calls."""
    return _borrow_adk_client(FrameworkToolClient(client_type()), client_type)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ModelTransportBinding:
    """Private configuration and a borrowed client for one authored model."""

    model: str
    _completion_args: Mapping[str, Any] = field(repr=False)
    _borrowed_client: Any = field(repr=False)

    def __init__(
        self,
        model: str,
        completion_args: Mapping[str, Any],
        borrowed_client: Any | None = None,
    ) -> None:
        """Snapshot arguments without copying provider clients or secret values."""

        object.__setattr__(self, "model", model)
        object.__setattr__(
            self, "_completion_args", MappingProxyType(dict(completion_args))
        )
        object.__setattr__(self, "_borrowed_client", borrowed_client)

    def __deepcopy__(self, memo: dict[int, Any]) -> "ModelTransportBinding":
        """Keep borrowed clients and identity intact when a framework copies metadata."""

        # Framework graph copies must not duplicate clients, controller locks,
        # or cleanup responsibilities hidden behind this immutable reference.
        memo[id(self)] = self
        return self

    def same_authority_as(self, other: "ModelTransportBinding") -> bool:
        """Compare static routing safely while keeping clients identity-bound."""

        return (
            type(other) is type(self)
            and self._borrowed_client is other._borrowed_client
            and _same_transport_value(self._transport_options(), other._transport_options())
        )

    def _transport_options(self) -> dict[str, Any]:
        """Exclude generation settings from the transport ownership boundary."""

        return {
            key: value for key, value in self._completion_args.items()
            if key in _TRANSPORT_ARGUMENTS
        }

    @property
    def transport_options(self) -> dict[str, Any]:
        """Transport and auth settings an evaluator judge may share with this model."""

        return self._transport_options()

    @property
    def borrowed_client(self) -> Any:
        """The model's client, which a judge may use but never closes."""

        return self._borrowed_client


def _same_transport_value(left: Any, right: Any) -> bool:
    """Never invoke user-defined equality to authorize transport sharing."""

    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if type(left) in (str, bytes, int, float, bool, type(None)):
        return left == right
    if type(left) in (dict, MappingProxyType):
        return _same_transport_mapping(left, right)
    # Clients, callbacks, auth wrappers, and other opaque values can carry
    # distinct authority even when their custom __eq__ claims equivalence.
    return False


def _same_transport_mapping(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare static option/header mappings with ordinary string keys only."""

    if any(type(key) is not str for key in (*left, *right)):
        return False
    return left.keys() == right.keys() and all(
        _same_transport_value(value, right[key]) for key, value in left.items()
    )


def _borrow_adk_client(client: Any, client_type: type[Any]) -> Any:
    """Bridge a LangGraph controller to ADK's validated client interface."""

    if isinstance(client, client_type):
        # ADK lifecycle clients already satisfy the adapter's concrete type.
        return client

    class BorrowedADKClient(client_type):
        """Forward completions without exposing lifecycle cleanup ownership."""

        async def acompletion(self, *args: Any, **kwargs: Any) -> Any:
            """Reuse the owner's asynchronous hooks and lazy transport."""

            return await client.acompletion(*args, **kwargs)

        def completion(self, *args: Any, **kwargs: Any) -> Any:
            """Reuse the owner's synchronous hooks and lazy transport."""

            return client.completion(*args, **kwargs)

    return BorrowedADKClient()


def model_transport_bindings(target: Any) -> tuple[ModelTransportBinding, ...]:
    """Retrieve bindings from an adapter or an enclosing runtime target."""

    bindings = getattr(target, _BINDINGS_ATTRIBUTE, ())
    if not isinstance(bindings, (tuple, list)):
        return ()
    return tuple(bindings)


def _attach_binding(target: Any, binding: ModelTransportBinding) -> Any:
    """Attach once by identity so propagated references do not become copies."""

    current = model_transport_bindings(target)
    if not any(value is binding for value in current):
        object.__setattr__(target, _BINDINGS_ATTRIBUTE, (*current, binding))
    return target


def attach_model_transport_binding(
    target: Any,
    *,
    model: str,
    completion_args: Mapping[str, Any],
    borrowed_client: Any | None = None,
) -> Any:
    """Capture only explicit transport configuration or an owned lifecycle."""

    if borrowed_client is None and not _TRANSPORT_ARGUMENTS.intersection(
        completion_args
    ):
        # Environment-only models retain their ordinary provider resolution;
        # a binding denotes an explicit transport choice worth borrowing.
        return target
    return _attach_binding(
        target, ModelTransportBinding(model, completion_args, borrowed_client)
    )


def propagate_model_transport_bindings(source: Any, target: Any) -> Any:
    """Carry borrowed transport metadata through framework wrapper objects."""

    for binding in model_transport_bindings(source):
        _attach_binding(target, binding)
    return target


__all__ = [
    "ModelTransportBinding",
    "attach_model_transport_binding",
    "model_transport_bindings",
    "propagate_model_transport_bindings",
]
