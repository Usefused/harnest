"""Framework-native suspension metadata for durable managed tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import functools
import hashlib
import inspect
import re
from typing import Annotated, Any, Iterator, Literal


_ARTIFACT_VERSION = "harnest-continuation/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_NATIVE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,2047}$")


@dataclass(frozen=True, slots=True)
class ResumeArtifact:
    """Private framework identity required to resume one persisted tool call."""

    framework: Literal["adk", "langgraph"]
    native_invocation_id: str
    tool_call_id: str
    tool_name: str
    version: str = _ARTIFACT_VERSION

    def __post_init__(self) -> None:
        """Reject identities that cannot safely cross storage backends."""

        if self.framework not in {"adk", "langgraph"}:
            raise ValueError("durable resume framework must be adk or langgraph")
        for name, value in (
            ("tool_call_id", self.tool_call_id),
            ("tool_name", self.tool_name),
            ("version", self.version),
        ):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"durable resume {name} is invalid")
        # LangGraph's managed thread id contains the encoded ownership scope
        # and can exceed the smaller identifiers used in public declarations.
        if not isinstance(
            self.native_invocation_id, str
        ) or not _NATIVE_IDENTIFIER.fullmatch(self.native_invocation_id):
            raise ValueError("durable resume native_invocation_id is invalid")

    def public_dict(self) -> dict[str, str]:
        """Serialize only stable framework identity for durable persistence."""

        return {
            "framework": self.framework,
            "native_invocation_id": self.native_invocation_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResumeArtifact":
        """Restore persisted identity without accepting extra compatibility fields."""

        expected = {
            "framework",
            "native_invocation_id",
            "tool_call_id",
            "tool_name",
            "version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("durable resume artifact fields are invalid")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class NativeResumeInput:
    """Private input that asks a framework driver to resume one saved call."""

    artifact: ResumeArtifact
    value: Any

    def __post_init__(self) -> None:
        """Require compiler-owned identity before bypassing ordinary user input."""

        if not isinstance(self.artifact, ResumeArtifact):
            raise TypeError("native resume input requires a ResumeArtifact")


class NativeDurableSuspended(RuntimeError):
    """Control flow raised after a framework durably records an external wait."""


@dataclass(frozen=True, slots=True)
class NativeDurableCall:
    """Active native call identity inherited by plugin continuation ports."""

    artifact: ResumeArtifact

    def submission_key(
        self,
        *,
        application_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        provider: str,
        capability: str,
    ) -> str:
        """Derive a stable opaque key that survives framework node replay."""

        # The native tool-call id is stable across framework replay. Binding all
        # ownership fields prevents a provider from accidentally deduplicating
        # two tenants or applications onto the same external submission.
        values = (
            application_id,
            user_id,
            session_id,
            run_id,
            provider,
            capability,
            self.artifact.framework,
            self.artifact.native_invocation_id,
            self.artifact.tool_call_id,
        )
        encoded = "\0".join(values).encode("utf-8")
        return "harnest-submission-v1." + hashlib.sha256(encoded).hexdigest()

    def suspend(self, pending: Any) -> Any:
        """Enter the selected framework's persisted suspension primitive."""

        if self.artifact.framework == "adk":
            # ADK suppresses its FunctionResponse only when the long-running
            # tool returns no value. Control flow prevents authored statements
            # after the wait from observing a misleading pending dictionary.
            raise NativeDurableSuspended
        from langgraph.types import interrupt

        return interrupt(pending.public())


_ACTIVE_NATIVE_CALL: ContextVar[NativeDurableCall | None] = ContextVar(
    "harnest_native_durable_call", default=None
)


def current_native_durable_call() -> NativeDurableCall | None:
    """Return the active framework call without exposing mutation authority."""

    return _ACTIVE_NATIVE_CALL.get()


@contextmanager
def native_durable_call(artifact: ResumeArtifact) -> Iterator[None]:
    """Bind native identity only for the dynamic extent of one durable tool."""

    call = NativeDurableCall(artifact)
    token = _ACTIVE_NATIVE_CALL.set(call)
    try:
        yield
    finally:
        _ACTIVE_NATIVE_CALL.reset(token)


def is_native_suspension(error: BaseException) -> bool:
    """Identify LangGraph's control-flow exception without a hard dependency."""

    error_type = type(error)
    return (
        error_type.__name__ == "GraphInterrupt"
        and error_type.__module__.startswith("langgraph.")
    )


def is_durable_tool(value: Any) -> bool:
    """Read the marker propagated through Harnest and framework wrappers."""

    return getattr(value, "__harnest_durable_tool__", False) is True


def adk_durable_tool(value: Callable[..., Any]) -> Any:
    """Lower a durable callable to ADK's native long-running tool boundary."""

    if not is_durable_tool(value):
        return value
    try:
        from google.adk.tools import LongRunningFunctionTool
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("durable ADK tools require google-adk") from exc

    class _HarnestLongRunningFunctionTool(LongRunningFunctionTool):
        """Bind ADK's persisted invocation identity around authored execution."""

        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            artifact = _adk_resume_artifact(tool_context, self.name)
            with native_durable_call(artifact):
                try:
                    return await super().run_async(args=args, tool_context=tool_context)
                except NativeDurableSuspended:
                    return None

    return _HarnestLongRunningFunctionTool(value)


def langgraph_durable_callable(value: Callable[..., Any]) -> Callable[..., Any]:
    """Inject LangGraph's native tool and thread identities into a callable."""

    if not is_durable_tool(value):
        return value
    try:
        from langchain_core.runnables import RunnableConfig
        from langchain_core.tools import InjectedToolCallId
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError(
            "durable LangGraph tools require langchain-core"
        ) from exc

    # LangChain currently treats the canonical name specially when it copies
    # the model ToolCall id into an injected argument. Fail during lowering if
    # authored input claimed that reserved framework boundary.
    call_id_name = "tool_call_id"
    if call_id_name in inspect.signature(value).parameters:
        raise TypeError("durable LangGraph tools reserve tool_call_id")
    config_name = _private_parameter_name(value, "_harnest_runnable_config")

    @functools.wraps(value)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        tool_call_id = kwargs.pop(call_id_name)
        config = kwargs.pop(config_name)
        artifact = ResumeArtifact(
            framework="langgraph",
            native_invocation_id=_langgraph_thread_id(config),
            tool_call_id=_required_identity(tool_call_id, "tool_call_id"),
            tool_name=_tool_name(value),
        )
        with native_durable_call(artifact):
            return await value(*args, **kwargs)

    signature = inspect.signature(value)
    injected = (
        inspect.Parameter(
            call_id_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[str, InjectedToolCallId],
        ),
        inspect.Parameter(
            config_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=RunnableConfig,
        ),
    )
    invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=_insert_keyword_parameters(signature, injected)
    )
    # LangChain reads type hints separately from inspect.signature when it
    # decides which arguments are runtime-injected and hidden from the model.
    invoke.__annotations__ = {
        **getattr(value, "__annotations__", {}),
        call_id_name: Annotated[str, InjectedToolCallId],
        config_name: RunnableConfig,
    }
    return invoke


def _adk_resume_artifact(tool_context: Any, tool_name: str) -> ResumeArtifact:
    """Read the two ADK identities required by a later FunctionResponse."""

    # ToolContext exposes these stable identities directly. Reading its private
    # InvocationContext would couple Harnest to ADK's session proxy internals.
    invocation_id = getattr(tool_context, "invocation_id", None)
    function_call_id = getattr(tool_context, "function_call_id", None)
    return ResumeArtifact(
        framework="adk",
        native_invocation_id=_required_identity(invocation_id, "invocation_id"),
        tool_call_id=_required_identity(function_call_id, "function_call_id"),
        tool_name=tool_name,
    )


def _langgraph_thread_id(config: Any) -> str:
    """Read the checkpoint thread used by LangGraph's Command(resume=...)."""

    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(configurable, Mapping):
        raise RuntimeError("durable LangGraph tool requires configurable thread_id")
    return _required_identity(configurable.get("thread_id"), "thread_id")


def _required_identity(value: Any, name: str) -> str:
    """Fail closed when a framework omits a persistence correlation field."""

    if not isinstance(value, str) or not value:
        raise RuntimeError(f"durable framework tool requires {name}")
    return value


def _tool_name(value: Callable[..., Any]) -> str:
    """Return the model-visible callable identity used for native resumption."""

    name = getattr(value, "__name__", None)
    if not isinstance(name, str) or not name:
        raise TypeError("durable tool must expose a stable __name__")
    return name


def _private_parameter_name(value: Callable[..., Any], preferred: str) -> str:
    """Choose a collision-free name for one framework-injected parameter."""

    existing = inspect.signature(value).parameters
    name = preferred
    while name in existing:
        name = "_" + name
    return name


def _insert_keyword_parameters(
    signature: inspect.Signature,
    injected: tuple[inspect.Parameter, ...],
) -> list[inspect.Parameter]:
    """Insert hidden keyword parameters before an authored **kwargs slot."""

    parameters = list(signature.parameters.values())
    for index, parameter in enumerate(parameters):
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return [*parameters[:index], *injected, *parameters[index:]]
    return [*parameters, *injected]


__all__ = [
    "NativeDurableSuspended",
    "NativeDurableCall",
    "NativeResumeInput",
    "ResumeArtifact",
    "adk_durable_tool",
    "current_native_durable_call",
    "is_durable_tool",
    "langgraph_durable_callable",
    "native_durable_call",
]
