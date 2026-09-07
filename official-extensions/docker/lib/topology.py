"""Immutable authoring contracts for extension-owned Docker topologies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Any

from harnest.sandbox import SandboxBudget, SandboxPolicyUnsupportedError


_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_SERVICES = 8
_MAX_ALIASES = 16
_MAX_PORTS = 32
_MAX_ENVIRONMENT = 64
_MAX_COMMAND_PARTS = 64
_MAX_TEXT_BYTES = 8 * 1024


class DockerScope(str, Enum):
    """Choose how long one owned Docker topology may be retained."""

    EXECUTION = "execution"
    INVOCATION = "invocation"
    SESSION = "session"


@dataclass(frozen=True, slots=True, kw_only=True)
class DockerNetwork:
    """Configure one extension-owned bridge shared only by this sandbox."""

    internal: bool = True

    def __post_init__(self) -> None:
        """Require an explicit boolean instead of truthy network authority."""

        if type(self.internal) is not bool:
            raise TypeError("Docker topology network internal must be a boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class DockerReadiness:
    """Wait for a declared Docker health command before sandbox execution."""

    command: tuple[str, ...]
    interval_seconds: float = 1.0
    timeout_seconds: float = 2.0
    retries: int = 30
    start_period_seconds: float = 0.0

    def __post_init__(self) -> None:
        """Bound the autonomous health command and its startup retry window."""

        object.__setattr__(
            self,
            "command",
            _command(self.command, label="Docker readiness command"),
        )
        for name in ("interval_seconds", "timeout_seconds"):
            _duration(name, getattr(self, name), allow_zero=False)
        _duration(
            "start_period_seconds",
            self.start_period_seconds,
            allow_zero=True,
        )
        if type(self.retries) is not int or not 1 <= self.retries <= 100:
            raise ValueError("Docker readiness retries must be an integer from 1 to 100")


@dataclass(frozen=True, slots=True, kw_only=True)
class DockerService:
    """Declare one bounded service container on the private sandbox network."""

    name: str
    image: str | None = None
    docker_path: str | None = None
    command: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    ports: tuple[int, ...] = ()
    aliases: tuple[str, ...] = ()
    readiness: DockerReadiness | None = None
    budget: SandboxBudget = field(default_factory=SandboxBudget)

    def __post_init__(self) -> None:
        """Freeze service input and reject unbounded or ambiguous Docker settings."""

        object.__setattr__(self, "name", _service_name(self.name, "service name"))
        if bool(self.image) == bool(self.docker_path):
            raise ValueError(
                "Docker service requires exactly one of image or docker_path"
            )
        _optional_text("service image", self.image)
        _optional_text("service docker_path", self.docker_path)
        object.__setattr__(
            self,
            "command",
            _command(self.command, label="Docker service command", required=False),
        )
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(self, "ports", _ports(self.ports))
        object.__setattr__(self, "aliases", _aliases(self.aliases))
        if self.readiness is not None and not isinstance(
            self.readiness, DockerReadiness
        ):
            raise TypeError("Docker service readiness must be DockerReadiness or None")
        if not isinstance(self.budget, SandboxBudget):
            raise TypeError("Docker service budget must be SandboxBudget")


def topology_config(
    services: tuple[DockerService, ...] | list[DockerService],
    network: DockerNetwork | None,
    *,
    external_network: bool,
) -> dict[str, Any] | None:
    """Validate one bounded topology and return a detached runtime snapshot."""

    normalized = _services(services)
    if not normalized:
        if network is not None:
            raise ValueError("Docker topology network requires at least one service")
        return None
    effective_network = DockerNetwork() if network is None else network
    if not isinstance(effective_network, DockerNetwork):
        raise TypeError("Docker topology network must be DockerNetwork or None")
    if not effective_network.internal and not external_network:
        raise SandboxPolicyUnsupportedError(
            "a non-internal Docker topology requires unrestricted network policy"
        )
    return {
        "network": {"internal": effective_network.internal},
        "services": [_service_config(service) for service in normalized],
    }


def _services(values: Any) -> tuple[DockerService, ...]:
    """Bound service count and keep every network identity unambiguous."""

    if not isinstance(values, (list, tuple)):
        raise TypeError("Docker services must be a list or tuple of DockerService")
    if len(values) > _MAX_SERVICES:
        raise ValueError(f"Docker topology supports at most {_MAX_SERVICES} services")
    if any(not isinstance(value, DockerService) for value in values):
        raise TypeError("Docker services must contain DockerService values")
    identities: list[str] = []
    for service in values:
        identities.extend((service.name, *service.aliases))
    if len(identities) != len(set(identities)):
        raise ValueError("Docker service names and aliases must be unique")
    return tuple(values)


def _service_config(service: DockerService) -> dict[str, Any]:
    """Detach immutable public values from the runtime's mutable SDK options."""

    readiness = service.readiness
    return {
        "name": service.name,
        "image": service.image,
        "docker_path": service.docker_path,
        "command": list(service.command),
        "environment": dict(service.environment),
        "ports": list(service.ports),
        "aliases": list(service.aliases),
        "readiness": None
        if readiness is None
        else {
            "command": list(readiness.command),
            "interval_seconds": readiness.interval_seconds,
            "timeout_seconds": readiness.timeout_seconds,
            "retries": readiness.retries,
            "start_period_seconds": readiness.start_period_seconds,
        },
        "budget": service.budget,
    }


def _service_name(value: Any, label: str) -> str:
    """Restrict service DNS identities to portable lowercase labels."""

    if not isinstance(value, str) or not _SERVICE_NAME.fullmatch(value):
        raise ValueError(
            f"Docker {label} must start with a letter and contain only "
            "lowercase letters, digits, and hyphens"
        )
    return value


def _aliases(values: Any) -> tuple[str, ...]:
    """Normalize a bounded set of additional service DNS labels."""

    if not isinstance(values, (list, tuple)):
        raise TypeError("Docker service aliases must be a list or tuple")
    if len(values) > _MAX_ALIASES:
        raise ValueError(f"Docker service supports at most {_MAX_ALIASES} aliases")
    aliases = tuple(_service_name(value, "service alias") for value in values)
    if len(aliases) != len(set(aliases)):
        raise ValueError("Docker service aliases must be unique")
    return aliases


def _ports(values: Any) -> tuple[int, ...]:
    """Validate declared container ports without creating host publications."""

    if not isinstance(values, (list, tuple)):
        raise TypeError("Docker service ports must be a list or tuple")
    if len(values) > _MAX_PORTS:
        raise ValueError(f"Docker service supports at most {_MAX_PORTS} ports")
    if any(type(value) is not int or not 1 <= value <= 65535 for value in values):
        raise ValueError("Docker service ports must contain integers from 1 to 65535")
    return tuple(dict.fromkeys(values))


def _environment(value: Any) -> dict[str, str]:
    """Copy bounded service environment without stringifying secret-like values."""

    if not isinstance(value, Mapping):
        raise TypeError("Docker service environment must be a string mapping")
    if len(value) > _MAX_ENVIRONMENT:
        raise ValueError(
            f"Docker service environment supports at most {_MAX_ENVIRONMENT} entries"
        )
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError("Docker service environment names must be portable names")
        if not isinstance(item, str):
            raise TypeError("Docker service environment values must be strings")
        _bounded_text("service environment value", item)
        result[name] = item
    return result


def _command(value: Any, *, label: str, required: bool = True) -> tuple[str, ...]:
    """Freeze an argv command and reject shell-like unbounded scalar input."""

    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list or tuple of text arguments")
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > _MAX_COMMAND_PARTS:
        raise ValueError(f"{label} supports at most {_MAX_COMMAND_PARTS} arguments")
    if value and (not isinstance(value[0], str) or not value[0]):
        raise ValueError(f"{label} executable must be non-empty text")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{label} arguments must be text")
        _bounded_text(label, item)
    return tuple(value)


def _optional_text(label: str, value: Any) -> None:
    """Validate optional image and build identifiers without normalizing them."""

    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Docker {label} must be non-empty text")
        _bounded_text(label, value)


def _bounded_text(label: str, value: str) -> None:
    """Cap authored strings before they enter persistent Docker configuration."""

    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"Docker {label} exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")


def _duration(name: str, value: Any, *, allow_zero: bool) -> None:
    """Match Docker's minimum health duration without accepting booleans."""

    valid_zero = allow_zero and value == 0
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or (not valid_zero and value < 0.001)
    ):
        qualifier = "zero or " if allow_zero else ""
        raise ValueError(
            f"Docker readiness {name} must be finite and {qualifier}at least 0.001"
        )


__all__ = [
    "DockerNetwork",
    "DockerReadiness",
    "DockerScope",
    "DockerService",
    "topology_config",
]
