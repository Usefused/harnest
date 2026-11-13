"""The in-tree registry of framework adapters.

``frameworks.json`` is the release-owned source of truth for framework names,
distributions, version ranges, and capabilities; the Go CLI embeds a copy of
the same file. This module pairs each entry with its Python adapter class.
Adding a framework means adding one ``frameworks.json`` entry, one adapter
package, and one line in :data:`_ADAPTERS`.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any

from .base import (
    Capability,
    DoctorModule,
    FrameworkAdapter,
    FrameworkMetadata,
    FrameworkName,
    UnknownFrameworkError,
)

# Adapter classes are imported lazily so configuration validation never pays
# for framework packages the project does not use.
_ADAPTERS: Mapping[FrameworkName, str] = MappingProxyType(
    {
        "adk": "harnest.frameworks.adk.adapter:ADKFramework",
        "langgraph": "harnest.frameworks.langgraph.adapter:LangGraphFramework",
    }
)


@cache
def _document() -> Mapping[str, Any]:
    """Read the packaged metadata document once per process."""

    text = resources.files(__package__).joinpath("frameworks.json").read_text("utf-8")
    return json.loads(text)


def _metadata_entry(entry: Mapping[str, Any]) -> FrameworkMetadata:
    """Convert one JSON entry into immutable metadata."""

    return FrameworkMetadata(
        name=entry["name"],
        display_name=entry["display_name"],
        distribution=entry["distribution"],
        versions=entry["versions"],
        install_extra=entry["install_extra"],
        requirements=tuple(entry["requirements"]),
        mcp_requirements=tuple(entry["mcp_requirements"]),
        doctor_modules=tuple(
            DoctorModule(module=item["module"], distribution=item["distribution"])
            for item in entry["doctor_modules"]
        ),
        capabilities=frozenset(Capability(value) for value in entry["capabilities"]),
    )


@cache
def framework_metadata() -> Mapping[FrameworkName, FrameworkMetadata]:
    """Return metadata for every registered framework in stable CLI order."""

    entries = (_metadata_entry(item) for item in _document()["frameworks"])
    return MappingProxyType({item.name: item for item in entries})


def shared_runtime_requirements() -> tuple[str, ...]:
    """Return runtime requirements every framework environment installs."""

    return tuple(_document()["shared_runtime_requirements"])


def framework_names() -> tuple[FrameworkName, ...]:
    """Return registered framework names in stable CLI order."""

    return tuple(framework_metadata())


def _unknown(name: object, subject: str = "framework") -> UnknownFrameworkError:
    """Explain the supported choices for an unregistered framework name."""

    choices = " or ".join(framework_names())
    return UnknownFrameworkError(f"{subject} must be {choices}; got {name!r}")


def require_framework(name: object, subject: str = "framework") -> FrameworkName:
    """Return a registered framework name, or reject it naming what was checked."""

    if isinstance(name, str) and name in framework_metadata():
        return name
    raise _unknown(name, subject)


@cache
def get_framework(name: FrameworkName) -> FrameworkAdapter:
    """Return the adapter for one framework without importing its SDK."""

    metadata = framework_metadata().get(name) if isinstance(name, str) else None
    path = _ADAPTERS.get(name) if metadata is not None else None
    if metadata is None or path is None:
        raise _unknown(name)
    module_name, _, class_name = path.partition(":")
    adapter_class = getattr(importlib.import_module(module_name), class_name)
    return adapter_class(metadata)


__all__ = [
    "framework_metadata",
    "framework_names",
    "get_framework",
    "shared_runtime_requirements",
]
