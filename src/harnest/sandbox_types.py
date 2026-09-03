"""Framework-independent contracts for isolated Python execution providers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SandboxFile:
    """Carry a file across an executor boundary without granting host access.

    String content is base64-encoded; bytes are the original file content.
    Providers own path validation and must never interpret names as host paths.
    """

    name: str = field(repr=False)
    content: str | bytes = field(repr=False)
    mime_type: str = "text/plain"

    def __post_init__(self) -> None:
        """Validate transport fields before native adapters serialize them."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("sandbox file name must be non-empty text")
        if not isinstance(self.content, (str, bytes)):
            raise TypeError("sandbox file content must be base64 text or bytes")
        if not isinstance(self.mime_type, str) or not self.mime_type.strip():
            raise ValueError("sandbox file MIME type must be non-empty text")


@dataclass(frozen=True, slots=True)
class SandboxContext:
    """Supply optional invocation identity without exposing a native runtime.

    Providers may use this identity for tenant isolation. Direct adapter calls
    outside a runtime have no identity; providers requiring it must reject them.
    """

    agent_name: str | None = None
    invocation_id: str | None = field(default=None, repr=False)
    user_id: str | None = field(default=None, repr=False)
    session_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """Describe one Python execution; the provider enforces its timeout."""

    code: str = field(repr=False)
    timeout_seconds: int | None = None
    context: SandboxContext = field(default_factory=SandboxContext)
    input_files: tuple[SandboxFile, ...] = field(default=(), repr=False)
    execution_id: str | None = field(default=None, repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Freeze caller-owned containers and reject invalid provider inputs."""
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("sandbox code must be non-empty text")
        validate_timeout(self.timeout_seconds)
        if not isinstance(self.context, SandboxContext):
            raise TypeError("sandbox context must be SandboxContext")
        object.__setattr__(self, "input_files", _files(self.input_files))
        object.__setattr__(self, "metadata", freeze_sandbox_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Return execution output without coupling providers to ADK or LangChain."""

    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    output_files: tuple[SandboxFile, ...] = field(default=(), repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate results before exposing them to the model's native loop."""
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("sandbox stdout and stderr must be text")
        object.__setattr__(self, "output_files", _files(self.output_files))
        object.__setattr__(self, "metadata", freeze_sandbox_metadata(self.metadata))


@runtime_checkable
class SandboxBackend(Protocol):
    """Execute in an isolated provider, never by evaluating code on the host.

    Implementations own isolation, deadlines, file handling, concurrency, resource
    cleanup, and user/session separation. Compilation never invokes this method.
    """

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute Python under provider-enforced policy and return its output."""
        ...


def validate_timeout(value: int | None) -> None:
    """Keep optional deadlines finite positive integers, excluding booleans."""
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("sandbox timeout_seconds must be a positive integer")


def _files(values: tuple[SandboxFile, ...]) -> tuple[SandboxFile, ...]:
    """Validate a bounded caller-supplied collection without retaining mutation."""
    if not isinstance(values, (list, tuple)):
        raise TypeError("sandbox files must be a list or tuple of SandboxFile")
    if any(not isinstance(value, SandboxFile) for value in values):
        raise TypeError("sandbox files must contain SandboxFile values")
    return tuple(values)


def freeze_sandbox_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """Snapshot JSON provider properties without retaining mutable SDK objects.

    Metadata is transport data, not native provider configuration. Rejecting
    unsupported values avoids silently changing their meaning by stringifying.
    """
    if not isinstance(metadata, Mapping):
        raise TypeError("sandbox metadata must be a JSON object")
    return _freeze_metadata_value(metadata, set())


def _freeze_metadata_value(value: Any, ancestors: set[int]) -> Any:
    """Preserve JSON scalar types and recursively freeze object and array data."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("sandbox metadata numbers must be finite")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        return _freeze_metadata_container(value, ancestors)
    raise TypeError("sandbox metadata values must be JSON-compatible")


def _freeze_metadata_container(value: Any, ancestors: set[int]) -> Any:
    """Reject true cycles while permitting shared, acyclic provider properties."""
    identity = id(value)
    if identity in ancestors:
        raise ValueError("sandbox metadata must not contain cycles")
    ancestors.add(identity)
    try:
        if isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise TypeError("sandbox metadata object keys must be strings")
            return MappingProxyType({
                key: _freeze_metadata_value(item, ancestors)
                for key, item in value.items()
            })
        return tuple(_freeze_metadata_value(item, ancestors) for item in value)
    finally:
        # Only the active ancestry is cyclic; sibling references are valid JSON.
        ancestors.remove(identity)


def sandbox_metadata_to_dict(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return fresh JSON-native metadata for a framework or provider transport."""
    return _thaw_metadata_value(freeze_sandbox_metadata(metadata))


def _thaw_metadata_value(value: Any) -> Any:
    """Restore objects and arrays without coercing booleans or numeric values."""
    if isinstance(value, Mapping):
        return {key: _thaw_metadata_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_metadata_value(item) for item in value]
    return value
