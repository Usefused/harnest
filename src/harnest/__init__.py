"""Public domain namespaces for Harnest agents."""

from importlib import import_module
from types import ModuleType


# Root exports are feature modules, never individual contracts. Classes,
# decorators, and functions remain under the domain that owns their public API.
__all__ = [
    "a2a",
    "agent",
    "application",
    "approval",
    "assets",
    "auth",
    "bundle",
    "checkpoint",
    "compatibility",
    "content",
    "context",
    "continuation",
    "credentials",
    "cron",
    "durable",
    "evaluation",
    "extensions",
    "graph",
    "http",
    "lifecycle",
    "logging",
    "mcp",
    "model",
    "orchestrator",
    "output",
    "plugins",
    "runtime",
    "sandbox",
    "server",
    "session",
    "skills",
    "store",
    "structured",
    "task",
    "telemetry",
    "testing",
    "tokens",
    "tracing",
]
_PUBLIC_DOMAINS = frozenset(__all__)


def __getattr__(name: str) -> ModuleType:
    """Load one public domain without eagerly importing the whole runtime."""

    if name not in _PUBLIC_DOMAINS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f".{name}", __name__)
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    """Include lazy public domains in interactive package discovery."""

    return sorted(set(globals()) | _PUBLIC_DOMAINS)
