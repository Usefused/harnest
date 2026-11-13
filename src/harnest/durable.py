"""Framework suspension metadata for durable managed tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterator

from .frameworks.registry import framework_names, require_framework
from .frameworks.base import FrameworkName


_ARTIFACT_VERSION = "harnest-continuation/v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_FRAMEWORK_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,2047}$")


@dataclass(frozen=True, slots=True)
class ResumeArtifact:
    """Private framework identity required to resume one persisted tool call."""

    framework: FrameworkName
    framework_invocation_id: str
    tool_call_id: str
    tool_name: str
    version: str = _ARTIFACT_VERSION

    def __post_init__(self) -> None:
        """Reject identities that cannot safely cross storage backends."""

        require_framework(self.framework, "durable resume framework")
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
            self.framework_invocation_id, str
        ) or not _FRAMEWORK_IDENTIFIER.fullmatch(self.framework_invocation_id):
            raise ValueError("durable resume framework_invocation_id is invalid")

    def public_dict(self) -> dict[str, str]:
        """Serialize only stable framework identity for durable persistence."""

        return {
            "framework": self.framework,
            # The v1 storage format keeps its original key so pending calls survive
            # the Python naming cleanup without changing replay identity.
            "native_invocation_id": self.framework_invocation_id,
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
        fields = dict(value)
        fields["framework_invocation_id"] = fields.pop("native_invocation_id")
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class FrameworkResumeInput:
    """Private input that asks a framework driver to resume one saved call."""

    artifact: ResumeArtifact
    value: Any

    def __post_init__(self) -> None:
        """Require compiler-owned identity before bypassing ordinary user input."""

        if not isinstance(self.artifact, ResumeArtifact):
            raise TypeError("framework resume input requires a ResumeArtifact")


class FrameworkDurableSuspended(RuntimeError):
    """Control flow raised after a framework durably records an external wait."""


@dataclass(frozen=True, slots=True)
class FrameworkDurableCall:
    """Active framework call identity inherited by plugin continuation ports."""

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
            self.artifact.framework_invocation_id,
            self.artifact.tool_call_id,
        )
        encoded = "\0".join(values).encode("utf-8")
        return "harnest-submission-v1." + hashlib.sha256(encoded).hexdigest()

    def suspend(self, pending: Any) -> Any:
        """Enter the selected framework's persisted suspension primitive."""

        from .frameworks.registry import get_framework

        return get_framework(self.artifact.framework).suspend_durable(pending)


_ACTIVE_FRAMEWORK_CALL: ContextVar[FrameworkDurableCall | None] = ContextVar(
    "harnest_framework_durable_call", default=None
)


def current_framework_durable_call() -> FrameworkDurableCall | None:
    """Return the active framework call without exposing mutation authority."""

    return _ACTIVE_FRAMEWORK_CALL.get()


@contextmanager
def framework_durable_call(artifact: ResumeArtifact) -> Iterator[None]:
    """Bind framework identity only for the dynamic extent of one durable tool."""

    call = FrameworkDurableCall(artifact)
    token = _ACTIVE_FRAMEWORK_CALL.set(call)
    try:
        yield
    finally:
        _ACTIVE_FRAMEWORK_CALL.reset(token)


def is_framework_suspension(error: BaseException) -> bool:
    """Ask adapters whether an exception is framework control flow."""

    from .frameworks.registry import get_framework

    return any(get_framework(name).is_suspension(error) for name in framework_names())


def is_durable_tool(value: Any) -> bool:
    """Read the marker propagated through Harnest and framework wrappers."""

    return getattr(value, "__harnest_durable_tool__", False) is True


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


__all__ = [
    "FrameworkDurableSuspended",
    "FrameworkDurableCall",
    "FrameworkResumeInput",
    "ResumeArtifact",
    "current_framework_durable_call",
    "is_durable_tool",
    "framework_durable_call",
]
