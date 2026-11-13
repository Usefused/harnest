"""Framework adapters: one package per supported agent framework.

``frameworks.json`` holds release-owned metadata shared with the Go CLI, and
:func:`get_framework` returns the adapter that implements
:class:`FrameworkAdapter` for one framework. Shared Harnest code calls adapter
hooks instead of branching on framework names.

To add a framework, start with the module docstring of ``base.py``: it lists
every hook, when Harnest calls it, and the steps to register a new adapter.
``adk/adapter.py`` and ``langgraph/adapter.py`` are complete examples.
"""

from .base import (
    AdvancedTarget,
    AdvancedTargetError,
    Capability,
    FrameworkAdapter,
    FrameworkConventionError,
    FrameworkDependencyError,
    FrameworkMetadata,
    FrameworkName,
    RuntimeSessions,
    UnknownFrameworkError,
    UnsupportedFrameworkFeature,
)
from .registry import (
    framework_metadata,
    framework_names,
    get_framework,
    require_framework,
    shared_runtime_requirements,
)

__all__ = [
    "AdvancedTarget",
    "AdvancedTargetError",
    "Capability",
    "FrameworkAdapter",
    "FrameworkConventionError",
    "FrameworkDependencyError",
    "FrameworkMetadata",
    "FrameworkName",
    "RuntimeSessions",
    "UnknownFrameworkError",
    "UnsupportedFrameworkFeature",
    "framework_metadata",
    "framework_names",
    "get_framework",
    "require_framework",
    "shared_runtime_requirements",
]
