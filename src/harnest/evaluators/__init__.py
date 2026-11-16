"""Evaluation engines behind Harnest's unified evaluation API.

Authors write one set of evaluation inputs whatever their framework: ``evals/``
files, ``harnest test --evals``, Playground evals, ``@metric`` scorers and
custom runners. An engine executes them. Each framework adapter names its
engine with ``FrameworkAdapter.evaluator()``, so a framework can bring its own
engine (for example LangSmith for LangGraph) without changing that API.

Engines live in subpackages (``harnest.evaluators.adk``). Shared Harnest code
reaches them only through the adapter, never by importing one directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..bundle import EvalSuite


@dataclass(frozen=True, slots=True)
class EvalResultRow:
    """One metric's result for one evaluated invocation, as the Playground shows it."""

    eval_id: str
    metric: str
    threshold: float | None
    score: float | None
    status: str
    prompt: str
    expected_response: str
    actual_response: str
    expected_tool_calls: str
    actual_tool_calls: str


class Evaluator(ABC):
    """One engine that runs Harnest's evaluation inputs and reports its results."""

    @abstractmethod
    def file_schemas(self) -> tuple[type[Any], type[Any]]:
        """Return the eval-set and test-config models that validate ``evals/`` files."""

    @abstractmethod
    def supported_metric_names(self) -> tuple[str, ...]:
        """Return the built-in metric names authors can list in ``criteria``."""

    @abstractmethod
    def configured_metrics(self, suite: EvalSuite) -> list[dict[str, Any]]:
        """Describe each effective criterion as ``name``, ``threshold`` and ``custom``."""

    @abstractmethod
    def run_suite(
        self,
        framework: str,
        suite: EvalSuite,
        eval_target: Callable[[], AbstractAsyncContextManager[tuple[str, Any]]],
        *,
        trajectory: str,
        print_results: bool,
        result_output: Path | None,
        scope: Callable[[], AbstractContextManager[Any]] = nullcontext,
        check_config: Callable[[Any], None] | None = None,
    ) -> int:
        """Score a suite for ``harnest test --evals`` and publish the result file.

        The adapter supplies what differs by framework: ``eval_target`` makes the
        agent importable for the engine and yields its module name and the
        target whose models the engine borrows; ``scope`` wraps the run;
        ``check_config`` rejects settings the framework cannot run. Returns the
        process exit code.
        """

    @abstractmethod
    def prepare_playground_set(self, path: Path, suite: EvalSuite, trajectory: str) -> Any:
        """Load one eval set and its criteria for a Playground run; expose ``case_ids``."""

    @abstractmethod
    async def run_playground_set(
        self,
        prepared: Any,
        *,
        agent_module: Callable[[], AbstractContextManager[str]],
        target: Any,
    ) -> tuple[tuple[EvalResultRow, ...], str | None]:
        """Run a prepared set; return its result rows and any scored-failure message."""


__all__ = ["EvalResultRow", "Evaluator"]
