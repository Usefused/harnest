"""Privacy-safe distinction between native execution errors and scored failures."""

from contextlib import contextmanager
from typing import Any


class EvaluationExecutionError(RuntimeError):
    """Evaluation could not execute reliably; quality thresholds are not the cause."""


LEGACY_SHORT_CIRCUIT_MESSAGE = (
    "Evaluation cannot safely use a replacement response returned by a native ADK "
    "before_run_callback on a legacy workflow agent. That runner does not guarantee "
    "context cleanup if the response stream is interrupted. Return None from the "
    "callback and produce the response in the agent or a tool instead. This is an "
    "evaluation configuration error, not a failed task-quality check."
)


class LegacyEvalShortCircuitError(EvaluationExecutionError):
    """A native legacy short-circuit cannot guarantee invocation cleanup."""


def execution_error_message(error: BaseException | None = None) -> str:
    """Explain the corrective action without echoing provider or tool payloads."""
    from .context import ContextUnavailableError

    if _has_cause(error, LegacyEvalShortCircuitError):
        return LEGACY_SHORT_CIRCUIT_MESSAGE
    if _has_cause(error, ContextUnavailableError):
        return (
            "Evaluation could not run the agent because its Harnest invocation context "
            "was unavailable. Tools such as list_skills and session-aware audit logging "
            "need this context. This is an evaluation integration error, not a failed "
            "task-quality check. Run evaluations through Harnest's managed evaluation "
            "entrypoint; if it persists there, report a Harnest integration bug."
        )
    return (
        "Evaluation could not complete agent execution and scoring. This is an "
        "execution error, not evidence that the agent failed a task-quality check. "
        "Check the local error traceback for the failing tool, callback, model provider, "
        "or runtime configuration, correct that issue, and rerun the evaluation."
    )


def _has_cause(error: BaseException | None, expected: type[BaseException]) -> bool:
    """Recognize ADK-wrapped callback failures without exposing exception text."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(seen) < 20:
        if isinstance(error, expected):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def evaluation_error_plugin() -> Any:
    """Observe unhandled native errors without intercepting recoverable tool errors."""
    from google.adk.plugins import BasePlugin

    class EvaluationErrors(BasePlugin):
        """Keep only redacted diagnostics for one isolated evaluator app."""

        def __init__(self):
            """Do not retain exception objects containing user code or credentials."""
            super().__init__(name="_harnest_eval_errors")
            self.message: str | None = None

        async def on_run_error_callback(self, *, invocation_context: Any, error: Exception) -> None:
            """Retain a safe explanation before ADK converts errors to assertions."""
            self.message = execution_error_message(error)

    return EvaluationErrors()


@contextmanager
def evaluation_error_boundary(observer: Any):
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
