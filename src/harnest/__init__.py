"""Public domain namespaces for Harnest agents."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING


# IDEs need named modules; runtime imports remain lazy to keep optional backends optional.
if TYPE_CHECKING:
    from . import a2a as a2a
    from . import agent as agent
    from . import application as application
    from . import assets as assets
    from . import auth as auth
    from . import authoring as authoring
    from . import bundle as bundle
    from . import checkpoint as checkpoint
    from . import compatibility as compatibility
    from . import content as content
    from . import context as context
    from . import continuation as continuation
    from . import credentials as credentials
    from . import cron as cron
    from . import decisions as decisions
    from . import durable as durable
    from . import evaluation as evaluation
    from . import extensions as extensions
    from . import graph as graph
    from . import http as http
    from . import lifecycle as lifecycle
    from . import logging as logging
    from . import mcp as mcp
    from . import memory as memory
    from . import model as model
    from . import orchestrator as orchestrator
    from . import output as output
    from . import runtime as runtime
    from . import sandbox as sandbox
    from . import server as server
    from . import session as session
    from . import skills as skills
    from . import store as store
    from . import structured as structured
    from . import task as task
    from . import telemetry as telemetry
    from . import testing as testing
    from . import tokens as tokens
    from . import tracing as tracing
    from . import ui as ui


# Root exports are feature modules, never individual contracts. Classes,
# decorators, and functions remain under the domain that owns their public API.
__all__ = [
    "a2a",
    "agent",
    "application",
    "assets",
    "auth",
    "authoring",
    "bundle",
    "checkpoint",
    "compatibility",
    "content",
    "context",
    "continuation",
    "credentials",
    "cron",
    "decisions",
    "durable",
    "evaluation",
    "extensions",
    "graph",
    "http",
    "lifecycle",
    "logging",
    "mcp",
    "memory",
    "model",
    "orchestrator",
    "output",
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
    "ui",
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
