import asyncio
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.context_agent import AgentResponse
from harnest.runtime import (
    _compiled_cli_enabled,
    _read_local_message,
    _run_command,
)
from harnest.runtime_cli import run_local_cli


class _Session:
    def __init__(self, items=(), response=None):
        self._items = tuple(items)
        self._response = response

    async def invoke(self, _message):
        return self._response

    async def stream(self, _message):
        for item in self._items:
            yield item


class _Runtime:
    def __init__(self, session):
        self.session = session
        self.opened = None

    async def create_session(self):
        return self.session

    async def open_session(self, session_id):
        self.opened = session_id
        return self.session


def _response(text="done"):
    return AgentResponse(
        output_text=text,
        result=None,
        events=(),
        session_id="session",
        invocation_id="invocation",
        metadata={},
    )


class RuntimeCLITests(unittest.TestCase):
    def test_text_output_keeps_tool_progress_on_stderr(self):
        items = (
            SimpleNamespace(
                kind="event",
                event={"type": "message", "text": "Checking "},
            ),
            SimpleNamespace(
                kind="event",
                event={"type": "tool_call", "name": "lookup", "arguments": {"secret": "hidden"}},
            ),
            SimpleNamespace(
                kind="event",
                event={"type": "tool_result", "name": "lookup", "result": "hidden"},
            ),
            SimpleNamespace(kind="completed", response=_response("Checking done")),
        )
        stdout = StringIO()
        stderr = StringIO()

        asyncio.run(
            run_local_cli(
                _Runtime(_Session(items)),
                "private prompt",
                session_id=None,
                output="text",
                stdout=stdout,
                stderr=stderr,
            )
        )

        self.assertEqual(stdout.getvalue(), "Checking \n")
        self.assertEqual(
            stderr.getvalue(), "[tool] lookup running\n[tool] lookup completed\n"
        )
        self.assertNotIn("hidden", stderr.getvalue())
        self.assertNotIn("private prompt", stderr.getvalue())

    def test_json_reopens_requested_session_and_writes_one_record(self):
        runtime = _Runtime(_Session(response=_response()))
        stdout = StringIO()

        asyncio.run(
            run_local_cli(
                runtime,
                "hello",
                session_id="existing",
                output="json",
                stdout=stdout,
                stderr=StringIO(),
            )
        )

        self.assertEqual(runtime.opened, "existing")
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertIn('"outputText":"done"', stdout.getvalue())

    def test_local_message_is_required_and_bounded(self):
        self.assertEqual(_read_local_message(StringIO("hello\n")), "hello\n")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            _read_local_message(StringIO(" \n"))
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            _read_local_message(StringIO("x" * (4 * 1024 * 1024 + 1)))

    def test_compiled_cli_policy_accepts_explicit_off_and_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            manifest = artifact / "harnest-manifest.json"
            manifest.write_text(json.dumps({"interfaces": {"cli": False}}))
            self.assertFalse(_compiled_cli_enabled(artifact))

            manifest.write_text(json.dumps({"interfaces": {"cli": True}}))
            self.assertTrue(_compiled_cli_enabled(artifact))

    def test_compiled_cli_policy_rejects_malformed_or_ambiguous_values(self):
        invalid = (
            '{"kind":"CompiledAgent"}',
            '{"interfaces":{"cli":"yes"}}',
            '{"interfaces":{"cli":true,"shell":true}}',
            '{"interfaces":{"cli":false,"cli":true}}',
        )
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            manifest = artifact / "harnest-manifest.json"
            for contents in invalid:
                with self.subTest(contents=contents):
                    manifest.write_text(contents)
                    with self.assertRaisesRegex(RuntimeError, "compiled manifest"):
                        _compiled_cli_enabled(artifact)

    def test_run_rejects_disabled_cli_before_reading_prompt(self):
        args = SimpleNamespace(artifact=Path("/unused"))
        stderr = StringIO()
        with (
            patch("harnest.runtime._compiled_cli_enabled", return_value=False),
            patch(
                "harnest.runtime._read_local_message",
                side_effect=AssertionError("prompt must not be read"),
            ),
            redirect_stderr(stderr),
        ):
            status = _run_command(args)

        self.assertEqual(status, 2)
        self.assertIn("spec.interfaces.cli: true", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
