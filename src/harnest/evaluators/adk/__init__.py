"""The ADK evaluation engine: Harnest's eval files scored by Google ADK's evaluation library.

Both built-in framework adapters use it today. Its modules load lazily, so
naming the engine never imports the ADK SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import EvalResultRow, Evaluator

if TYPE_CHECKING:
    from ...bundle import EvalSuite


class ADKEvaluator(Evaluator):
    """Run evaluations with ``google.adk.evaluation``."""

    def file_schemas(self) -> tuple[type[Any], type[Any]]:
        """Return ADK's ``EvalSet`` and ``EvalConfig`` models."""
        from .config import _eval_file_schemas

        return _eval_file_schemas()

    def supported_metric_names(self) -> tuple[str, ...]:
        """Reflect the installed ADK metric registry."""
        from .config import supported_metric_names

        return supported_metric_names()

    def configured_metrics(self, suite: EvalSuite) -> list[dict[str, Any]]:
        """Describe effective authored and default criteria and custom registrations."""
        from .playground import _configured_metrics

        return _configured_metrics(suite)

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
        """Score the suite with ADK's ``AgentEvaluator`` and publish the result."""
        from .suite import _run_eval_suite

        return _run_eval_suite(
            framework, suite, eval_target, trajectory=trajectory,
            print_results=print_results, result_output=result_output,
            scope=scope, check_config=check_config,
        )

    def prepare_playground_set(self, path: Path, suite: EvalSuite, trajectory: str) -> Any:
        """Load the evaluator, the eval set and its criteria."""
        from .playground import prepare_playground_set

        return prepare_playground_set(path, suite, trajectory)

    async def run_playground_set(
        self,
        prepared: Any,
        *,
        agent_module: Callable[[], AbstractContextManager[str]],
        target: Any,
    ) -> tuple[tuple[EvalResultRow, ...], str | None]:
        """Run the prepared set and read ADK's result rows."""
        from .playground import run_playground_set

        return await run_playground_set(prepared, agent_module=agent_module, target=target)


evaluator = ADKEvaluator()
"""The shared ADK engine instance adapters return from ``evaluator()``."""


__all__ = ["ADKEvaluator", "evaluator"]
