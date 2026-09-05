"""Provider-neutral sandbox policy and portable resource budgets."""

from dataclasses import dataclass
from enum import Enum
import ipaddress
import math
import re


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class SandboxNetworkMode(str, Enum):
    """Describe the outbound network authority granted to one sandbox."""

    NONE = "none"
    UNRESTRICTED = "unrestricted"
    ALLOWLIST = "allowlist"


@dataclass(frozen=True, slots=True)
class SandboxNetworkPolicy:
    """Declare provider-enforced outbound access without assuming its mechanism.

    Allowlist entries are exact DNS names or IP literals. Providers must apply
    the policy to every connection and resolution, not only to the initial URL.
    """

    mode: SandboxNetworkMode = SandboxNetworkMode.NONE
    allow_hosts: tuple[str, ...] = ()
    allow_ports: tuple[int, ...] = ()
    block_private_networks: bool = True

    def __post_init__(self) -> None:
        """Normalize immutable entries and reject ambiguous network authority."""
        try:
            mode = SandboxNetworkMode(self.mode)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "sandbox network mode must be none, unrestricted, or allowlist"
            ) from error
        hosts = _network_hosts(self.allow_hosts)
        ports = _network_ports(self.allow_ports)
        if type(self.block_private_networks) is not bool:
            raise TypeError("sandbox block_private_networks must be a boolean")
        if mode != SandboxNetworkMode.ALLOWLIST and (hosts or ports):
            raise ValueError("sandbox network allowlists require allowlist mode")
        if mode == SandboxNetworkMode.ALLOWLIST and not hosts:
            raise ValueError("sandbox network allowlist mode requires allow_hosts")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "allow_hosts", hosts)
        object.__setattr__(self, "allow_ports", ports)

    @classmethod
    def none(cls) -> "SandboxNetworkPolicy":
        """Deny all outbound network connections."""
        return cls(mode=SandboxNetworkMode.NONE)

    @classmethod
    def unrestricted(
        cls, *, block_private_networks: bool = False,
    ) -> "SandboxNetworkPolicy":
        """Permit outbound access, optionally retaining a private-network ceiling."""
        return cls(
            mode=SandboxNetworkMode.UNRESTRICTED,
            block_private_networks=block_private_networks,
        )

    @classmethod
    def allowlist(
        cls,
        *hosts: str,
        ports: tuple[int, ...] = (),
        block_private_networks: bool = True,
    ) -> "SandboxNetworkPolicy":
        """Permit exact hosts and optional destination ports only."""
        return cls(
            mode=SandboxNetworkMode.ALLOWLIST,
            allow_hosts=hosts,
            allow_ports=ports,
            block_private_networks=block_private_networks,
        )


@dataclass(frozen=True, slots=True)
class SandboxProviderCapabilities:
    """Advertise network controls a provider enforces below agent code."""

    network_modes: frozenset[SandboxNetworkMode] = frozenset()
    host_allowlist: bool = False
    port_allowlist: bool = False
    private_network_blocking: bool = False

    def __post_init__(self) -> None:
        """Freeze declared modes and require explicit boolean guarantees."""
        try:
            modes = frozenset(SandboxNetworkMode(value) for value in self.network_modes)
        except (TypeError, ValueError) as error:
            raise ValueError("sandbox provider declares an unknown network mode") from error
        for name in ("host_allowlist", "port_allowlist", "private_network_blocking"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"sandbox provider {name} capability must be a boolean")
        object.__setattr__(self, "network_modes", modes)

    def require(self, policy: SandboxNetworkPolicy) -> None:
        """Fail closed when this provider cannot enforce requested authority."""
        if policy.mode not in self.network_modes:
            raise SandboxPolicyUnsupportedError(
                f"sandbox provider does not enforce network mode {policy.mode.value!r}"
            )
        if policy.mode == SandboxNetworkMode.ALLOWLIST and not self.host_allowlist:
            raise SandboxPolicyUnsupportedError(
                "sandbox provider does not enforce exact host allowlists"
            )
        if policy.allow_ports and not self.port_allowlist:
            raise SandboxPolicyUnsupportedError(
                "sandbox provider does not enforce destination port allowlists"
            )
        if (
            policy.mode != SandboxNetworkMode.NONE
            and policy.block_private_networks
            and not self.private_network_blocking
        ):
            raise SandboxPolicyUnsupportedError(
                "sandbox provider does not block private, loopback, link-local, and metadata networks"
            )


class SandboxPolicyUnsupportedError(ValueError):
    """A provider cannot enforce an explicitly requested sandbox policy."""


def _network_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize exact host entries while rejecting URL and wildcard syntax."""
    if not isinstance(values, (list, tuple)):
        raise TypeError("sandbox network allow_hosts must be a list or tuple")
    hosts = tuple(_network_host(value) for value in values)
    return tuple(dict.fromkeys(hosts))


def _network_host(value: str) -> str:
    """Validate one DNS name or IP literal without triggering DNS resolution."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sandbox network allow_hosts entries must be non-empty text")
    host = value.strip().rstrip(".").lower()
    address = _network_address(host)
    if address is not None:
        return address
    if any(token in host for token in ("://", "/", "*", "@", ":")):
        raise ValueError("sandbox network allow_hosts entries must be exact hosts")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("sandbox network allow_hosts contains an invalid DNS name") from error
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("sandbox network allow_hosts contains an invalid DNS name")
    return ascii_host


def _network_address(host: str) -> str | None:
    """Normalize an IP literal, rejecting provider-local IPv6 scope identifiers."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    # Scope identifiers name an interface inside one provider, so they do not
    # have a portable exact-host meaning at this configuration boundary.
    if getattr(address, "scope_id", None) is not None:
        raise ValueError("sandbox network allow_hosts entries must be exact hosts")
    return address.compressed


def _network_ports(values: tuple[int, ...]) -> tuple[int, ...]:
    """Normalize destination ports and reject booleans and invalid ranges."""
    if not isinstance(values, (list, tuple)):
        raise TypeError("sandbox network allow_ports must be a list or tuple")
    if any(type(value) is not int or not 1 <= value <= 65535 for value in values):
        raise ValueError("sandbox network allow_ports must contain integers from 1 to 65535")
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class SandboxBudget:
    """Request finite compute, memory, process, and scratch-space limits."""

    cpu: float = 1.0
    memory_bytes: int = 512 * 1024 * 1024
    pids: int = 64
    scratch_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        """Reject unlimited, boolean, and non-finite resource values before startup."""
        if type(self.cpu) not in (int, float) or not math.isfinite(self.cpu) or self.cpu < 0.01:
            raise ValueError("sandbox budget cpu must be finite and at least 0.01")
        for name in ("memory_bytes", "pids", "scratch_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"sandbox budget {name} must be a positive integer")
        if self.memory_bytes < 6 * 1024 * 1024:
            raise ValueError("sandbox memory_bytes must be at least 6MiB")
