"""Structured lifecycle guarantees for managed and advanced applications."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Mapping, cast


class CoverageLevel(StrEnum):
    """Describe how completely Harnest governs one runtime boundary."""

    FULL = "full"
    WRAPPED_ONLY = "wrapped-only"
    BEST_EFFORT = "best-effort"
    FRAMEWORK_OWNED = "framework-owned"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LifecycleCoverage:
    """Stable report of lifecycle boundaries Harnest can actually guarantee."""

    framework: Literal["adk", "langgraph"]
    mode: Literal["managed", "advanced"]
    application: CoverageLevel
    http: CoverageLevel
    invocation: CoverageLevel
    credentials: CoverageLevel
    session: CoverageLevel
    assets: CoverageLevel
    model: CoverageLevel
    tool: CoverageLevel
    mcp: CoverageLevel
    agent: CoverageLevel
    subagent: CoverageLevel
    checkpoint: CoverageLevel

    def stage(self, name: str) -> CoverageLevel:
        """Return one declared stage without exposing implementation state."""

        if name not in _STAGES:
            raise KeyError(f"unknown lifecycle coverage stage {name!r}")
        return getattr(self, name)

    def report(self) -> Mapping[str, str]:
        """Return an immutable JSON-compatible diagnostics report."""

        values = asdict(self)
        return MappingProxyType(
            {
                name: str(value)
                for name, value in values.items()
            }
        )

    def with_overrides(
        self, overrides: Mapping[str, CoverageLevel]
    ) -> "LifecycleCoverage":
        """Record adapter-observed limits without changing mode defaults."""

        updates: dict[str, CoverageLevel] = {}
        for name, level in overrides.items():
            if name not in _STAGES:
                raise KeyError(f"unknown lifecycle coverage stage {name!r}")
            if not isinstance(level, CoverageLevel):
                raise TypeError("lifecycle coverage overrides must be CoverageLevel")
            updates[name] = level
        return replace(self, **updates)


_STAGES = frozenset(
    {
        "application",
        "http",
        "invocation",
        "credentials",
        "session",
        "assets",
        "model",
        "tool",
        "mcp",
        "agent",
        "subagent",
        "checkpoint",
    }
)


def lifecycle_coverage(
    framework: str,
    mode: str,
    *,
    overrides: Mapping[str, CoverageLevel] | None = None,
) -> LifecycleCoverage:
    """Build conservative defaults and apply adapter-observed coverage limits."""

    if framework not in {"adk", "langgraph"}:
        raise ValueError("lifecycle coverage framework must be adk or langgraph")
    if mode not in {"managed", "advanced"}:
        raise ValueError("lifecycle coverage mode must be managed or advanced")
    selected = cast(Literal["adk", "langgraph"], framework)
    coverage = (
        _managed_coverage(selected)
        if mode == "managed"
        else _advanced_coverage(selected)
    )
    return coverage.with_overrides(overrides or {})


def _managed_coverage(
    framework: Literal["adk", "langgraph"],
) -> LifecycleCoverage:
    """Report full coverage where Harnest constructs every managed boundary."""

    return LifecycleCoverage(
        framework=framework,
        mode="managed",
        application=CoverageLevel.FULL,
        http=CoverageLevel.FULL,
        invocation=CoverageLevel.FULL,
        credentials=CoverageLevel.FULL,
        session=CoverageLevel.FULL,
        assets=CoverageLevel.FULL,
        model=CoverageLevel.FULL,
        tool=CoverageLevel.FULL,
        # Native and context-originated calls share one revocable governed
        # marker, so neither managed dispatch path can bypass policy.
        mcp=CoverageLevel.FULL,
        agent=CoverageLevel.FULL,
        subagent=CoverageLevel.BEST_EFFORT,
        checkpoint=CoverageLevel.FULL,
    )


def _advanced_coverage(
    framework: Literal["adk", "langgraph"],
) -> LifecycleCoverage:
    """Keep Harnest-owned edges explicit while native internals stay native."""

    return LifecycleCoverage(
        framework=framework,
        mode="advanced",
        application=CoverageLevel.FULL,
        http=CoverageLevel.FULL,
        invocation=CoverageLevel.FULL,
        credentials=CoverageLevel.FULL,
        session=CoverageLevel.FULL,
        assets=CoverageLevel.FULL,
        model=CoverageLevel.WRAPPED_ONLY,
        tool=CoverageLevel.WRAPPED_ONLY,
        mcp=CoverageLevel.WRAPPED_ONLY,
        agent=CoverageLevel.WRAPPED_ONLY,
        subagent=CoverageLevel.BEST_EFFORT,
        checkpoint=CoverageLevel.FRAMEWORK_OWNED,
    )


__all__ = ["CoverageLevel", "LifecycleCoverage", "lifecycle_coverage"]
