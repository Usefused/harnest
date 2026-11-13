"""Public contracts for evaluation packages, re-exported from harnest.evaluation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx


@dataclass(frozen=True, slots=True)
class EvalRunnerContext:
    """One runner's options, agent HTTP client, project inputs, and private report directory."""

    client: httpx.Client
    project_dir: Path
    report_dir: Path
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EvalRunnerResult:
    """A quality verdict and optional report files relative to the runner's directory."""

    status: Literal["passed", "failed", "not_evaluated"]
    artifacts: tuple[str, ...] = ()
