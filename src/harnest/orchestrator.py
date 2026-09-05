"""Python-authored deployment plans consumed by the Go runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class AgentSource:
    """Select agent directories included in a deployment plan."""

    root: str
    include: Sequence[str] = field(default_factory=lambda: ("*",))
    exclude: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root.strip():
            raise ValueError("agent source root is required")
        if isinstance(self.include, (str, bytes)):
            raise TypeError("agent source include must be a sequence, not a string")
        if isinstance(self.exclude, (str, bytes)):
            raise TypeError("agent source exclude must be a sequence, not a string")

    @classmethod
    def directory(
        cls,
        root: str,
        *,
        include: Sequence[str] = ("*",),
        exclude: Sequence[str] = (),
    ) -> "AgentSource":
        """Create a directory source with optional include and exclude globs."""

        return cls(root=root, include=include, exclude=exclude)


@dataclass(frozen=True, slots=True)
class Orchestrator:
    """Describe a deterministic multi-agent deployment plan."""

    sources: Sequence[AgentSource | str]
    parallelism: int = 4
    fail_fast: bool = False
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.sources, (str, bytes)):
            raise TypeError("orchestrator sources must be a sequence, not a string")
        if not self.sources:
            raise ValueError("orchestrator requires at least one agent source")
        if self.parallelism < 1:
            raise ValueError("parallelism must be at least 1")

    def plan(self, *, project_root: str | Path) -> dict[str, Any]:
        """Build the JSON-compatible deployment plan for a project root."""

        root = Path(project_root).resolve()
        sources = []
        for source in self.sources:
            source = AgentSource.directory(source) if isinstance(source, str) else source
            sources.append(
                {
                    "root": source.root,
                    "include": list(source.include),
                    "exclude": list(source.exclude),
                }
            )
        return {
            "apiVersion": "harnest.dev/v1alpha1",
            "kind": "DeploymentPlan",
            "projectRoot": str(root),
            "parallelism": self.parallelism,
            "failFast": self.fail_fast,
            "labels": dict(self.labels),
            "sources": sources,
        }

    def to_json(self, *, project_root: str | Path) -> str:
        """Serialize the deployment plan as stable, human-readable JSON."""

        return json.dumps(self.plan(project_root=project_root), indent=2, sort_keys=True)


def define_orchestrator(
    *,
    agents: Sequence[AgentSource | str],
    parallelism: int = 4,
    fail_fast: bool = False,
    labels: Mapping[str, str] | None = None,
) -> Orchestrator:
    """Create an orchestrator from agent directory sources and plan options."""

    return Orchestrator(agents, parallelism=parallelism, fail_fast=fail_fast, labels=labels or {})
