"""Eval progress stays visible during awaits without changing results or failures."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from harnest.bundle import EvalSuite
from harnest.testing import _eval_progress, _eval_suite_progress, _evaluate_eval_sets


class EvalProgressTests(unittest.TestCase):
    def test_progress_is_flushed_to_stderr_and_quiet_mode_emits_nothing(self):
        with patch("builtins.print") as output:
            _eval_progress("Loading evaluator", enabled=True)
            _eval_progress("hidden", enabled=False)
        self.assertEqual(output.call_count, 1)
        self.assertTrue(output.call_args.kwargs["flush"])
        self.assertIn("file", output.call_args.kwargs)

    def test_suite_timing_preserves_failure_and_cancellation(self):
        for failure, outcome in ((None, "completed"), (AssertionError("score"), "failed"),
                                 (asyncio.CancelledError(), "stopped")):
            with self.subTest(outcome=outcome):
                output = StringIO()
                with redirect_stderr(output), patch("harnest.testing.time.monotonic", side_effect=(10, 12.5)):
                    try:
                        with _eval_suite_progress(1, 2, enabled=True):
                            self.assertIn("running agent and scoring", output.getvalue())
                            self.assertNotIn("2.5s", output.getvalue())
                            if failure is not None:
                                raise failure
                    except BaseException as error:
                        self.assertIs(error, failure)
                self.assertIn(f"Suite 1/2 {outcome} (2.5s)", output.getvalue())

    def test_eval_sets_report_progress_before_await_and_continue_after_scored_failure(self):
        errors, output = StringIO(), StringIO()
        calls = []

        async def evaluate(*, eval_set, **kwargs):
            """Check visible progress at the framework boundary before work finishes."""
            calls.append(eval_set.eval_set_id)
            self.assertIn(f"Suite {len(calls)}/2: running", errors.getvalue())
            self.assertEqual(kwargs["num_runs"], 1)
            self.assertNotIn(f"Suite {len(calls)}/2 completed", errors.getvalue())
            await asyncio.sleep(0)
            if len(calls) == 1:
                raise AssertionError("score below threshold")

        def decode(payload):
            """Read suite identity without requiring a model or provider service."""
            return SimpleNamespace(**json.loads(payload))

        with tempfile.TemporaryDirectory() as directory:
            paths = tuple(Path(directory) / f"{name}.evalset.json" for name in ("first", "second"))
            for path in paths:
                path.write_text(json.dumps({"eval_set_id": path.stem}), encoding="utf-8")
            with redirect_stderr(errors), redirect_stdout(output), self.assertRaisesRegex(AssertionError, "score below"):
                asyncio.run(_evaluate_eval_sets(
                    SimpleNamespace(evaluate_eval_set=evaluate),
                    SimpleNamespace(model_validate_json=decode),
                    module_name="agent", suite=EvalSuite(paths, None),
                    config=object(), trajectory="business",
                ))
        self.assertEqual(calls, ["first.evalset", "second.evalset"])
        self.assertIn("Suite 1/2 failed", errors.getvalue())
        self.assertIn("Suite 2/2 completed", errors.getvalue())
        self.assertNotIn("running agent", output.getvalue())


if __name__ == "__main__":
    unittest.main()
