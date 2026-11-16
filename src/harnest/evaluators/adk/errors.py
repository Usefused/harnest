"""ADK evaluation engine: tell native execution failures apart from scored failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ...eval_errors import EvaluationExecutionError, execution_error_message


def evaluation_error_plugin() -> Any:
    """Observe unhandled native errors without intercepting recoverable tool errors."""
    from google.adk.plugins import BasePlugin

    class EvaluationErrors(BasePlugin):
        """Keep only redacted diagnostics for one isolated evaluator app."""

        def __init__(self) -> None:
            """Do not retain exception objects containing user code or credentials."""
            super().__init__(name="_harnest_eval_errors")
            self.message: str | None = None

        async def on_run_error_callback(self, *, invocation_context: Any, error: Exception) -> None:
            """Retain a safe explanation before ADK converts errors to assertions."""
            self.message = execution_error_message(error)

    return EvaluationErrors()


@contextmanager
def evaluation_error_boundary(observer: Any) -> Iterator[None]:
    """Preserve genuine scored assertions but explain native execution failures."""
    try:
        yield
    except AssertionError as error:
        # ADK also records failures before a runner/plugin exists. Its unscored
        # failure summary is the fallback when no callback can observe them.
        # An earlier failed attempt may have recovered on retry. Only an
        # unscored final failure changes the verdict, not a scored assertion.
        if "without producing any metric results" in str(error):
            raise EvaluationExecutionError(
                observer.message or execution_error_message()
            ) from error
        raise
