"""Explicit Docker isolation budgets and identity scopes shared by both frameworks."""

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class SandboxBudget:
    """Hard Docker limits; scratch is the only writable filesystem allocation."""

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

    def docker_options(self) -> dict[str, Any]:
        """Apply kernel limits without permitting mounts or privilege overrides."""
        return {
            "nano_cpus": int(self.cpu * 1_000_000_000),
            "mem_limit": self.memory_bytes, "memswap_limit": self.memory_bytes,
            "pids_limit": self.pids, "read_only": True, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "user": "65534:65534",
            "working_dir": "/tmp", "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={self.scratch_bytes},mode=1777"},
        }


def scope_key(scope: str, context: Any) -> tuple[str, ...] | None:
    """Never use a caller-provided execution ID or a session ID without its owner."""
    if scope == "execution":
        return None
    fields = [context.agent_name, context.user_id, context.session_id]
    if scope == "invocation":
        fields.append(context.invocation_id)
    if any(not isinstance(value, str) or not value for value in fields):
        raise ValueError(f"sandbox {scope} scope requires agent, user, session" +
                         (", and invocation identity" if scope == "invocation" else " identity"))
    return tuple(fields)


def validate_scope(scope: str, max_scopes: int) -> None:
    """Bound retained containers and require an explicitly understood scope."""
    if scope not in ("execution", "invocation", "session"):
        raise ValueError("sandbox scope must be execution, invocation, or session")
    if type(max_scopes) is not int or max_scopes <= 0:
        raise ValueError("sandbox max_scopes must be a positive integer")
