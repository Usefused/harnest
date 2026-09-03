"""Borrow explicitly configured model transports without transferring ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


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

    def build_eval_model(self, model_name: str) -> Any:
        """Build an ADK adapter sharing transport but never cleanup ownership."""

        from google.adk.models.lite_llm import LiteLlm, LiteLLMClient

        # Agent generation settings must not replace a judge's temperature,
        # sampling, or output contract; only transport/auth choices are shared.
        arguments = {
            key: value
            for key, value in self._completion_args.items()
            if key in _TRANSPORT_ARGUMENTS
        }
        if self._borrowed_client is not None:
            # Hooks and lazy initialization belong to the original controller;
            # constructing another lifecycle would create a second transport.
            arguments["llm_client"] = _borrow_adk_client(
                self._borrowed_client, LiteLLMClient
            )
        # Explicit native `client` options remain untouched in the arguments.
        # In particular, no lifecycle resource is attached to this borrower.
        return LiteLlm(model=model_name, **arguments)


def _borrow_adk_client(client: Any, client_type: type[Any]) -> Any:
    """Bridge a LangGraph controller to ADK's validated client interface."""

    if isinstance(client, client_type):
        # ADK lifecycle clients already satisfy the adapter's concrete type.
        return client

    class BorrowedADKClient(client_type):
        """Forward completions without exposing lifecycle cleanup ownership."""

        async def acompletion(self, **kwargs: Any) -> Any:
            """Reuse the owner's asynchronous hooks and lazy transport."""

            return await client.acompletion(**kwargs)

        def completion(self, **kwargs: Any) -> Any:
            """Reuse the owner's synchronous hooks and lazy transport."""

            return client.completion(**kwargs)

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
