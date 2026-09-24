import asyncio
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.context_agent import AgentResponse
from harnest.bundle import compile_artifact
from harnest.runtime import (
    _compiled_cli_enabled,
    _read_local_message,
    _run_command,
    _runtime_parser,
)
from harnest.runtime_cli import run_local_cli
from harnest.runtime_task import TaskRuntimeError


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
    def test_compiled_run_explains_missing_task_storage(self):
        """The real artifact process retains the task provider's configuration hint."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            (root / "tasks").mkdir(parents=True)
            (root / "lifecycle").mkdir()
            (root / "lifecycle" / "storage.py").write_text(
                "from harnest import lifecycle\n"
                "from harnest.store import MemoryStore\n"
                "store = MemoryStore()\n"
                "@lifecycle.storage.sessions\n"
                "@lifecycle.storage.checkpoints\n"
                "def state_store():\n"
                "    return store\n"
            )
            (root / "agent.py").write_text(
                "from harnest.graph import START, Edge, Event, Graph\n"
                "def respond(value):\n"
                "    return Event(message='done')\n"
                "root_agent = Graph(name='root', nodes={'respond': respond}, "
                "edges=(Edge(START, 'respond'),))\n"
            )
            (root / "agent-card.yaml").write_text("name: CLI test\ndescription: Task diagnostics\n")
            (root / "tasks" / "work.py").write_text(
                "from harnest.task import task\n"
                "@task\n"
                "def work():\n"
                "    \"\"\"Return a deterministic task result.\"\"\"\n"
                "    return 'done'\n"
            )
            for framework in ("adk", "langgraph"):
                with self.subTest(framework=framework):
                    artifact = Path(directory) / framework
                    compile_artifact(root, artifact, framework=framework, cli_enabled=True)
                    result = subprocess.run(
                        [sys.executable, str(artifact / "harnest-agent"), "run", "hello"],
                        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")),
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("queued tasks require an explicit storage provider", result.stderr)
                    self.assertIn("@lifecycle.storage.tasks", result.stderr)
                    self.assertNotIn("local invocation failed with TaskRuntimeError", result.stderr)

    def test_run_preserves_safe_task_diagnostics_but_hides_provider_payloads(self):
        """Task diagnostics are safe; arbitrary provider exception text is not."""
        args = _runtime_parser().parse_args(["--artifact", "/unused", "run", "hello"])
        for failure, expected in (
            (TaskRuntimeError("task runtime startup failed with ConnectionError"),
             "task runtime startup failed with ConnectionError"),
            (RuntimeError("private provider request"),
             "local invocation failed with RuntimeError"),
        ):
            with self.subTest(failure=type(failure).__name__):
                stderr = StringIO()
                with (
                    patch("harnest.runtime._compiled_cli_enabled", return_value=True),
                    patch("harnest.runtime._run_local_artifact", side_effect=failure),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(_run_command(args), 1)
                self.assertEqual(stderr.getvalue(), f"harnest-agent: {expected}\n")

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

    def test_positional_message_runs_without_reading_stdin(self):
        """A native executable can accept a prompt while preserving authored whitespace."""
        args = _runtime_parser().parse_args(["--artifact", "/unused", "run", "  hello  "])
        seen = []

        async def invoke(_args, message):
            """Capture the validated message without constructing a provider runtime."""
            seen.append(message)

        with (
            patch("harnest.runtime._compiled_cli_enabled", return_value=True),
            patch("harnest.runtime._run_local_artifact", side_effect=invoke),
            patch("harnest.runtime.sys.stdin", None),
        ):
            self.assertEqual(_run_command(args), 0)
        self.assertEqual(seen, ["  hello  "])

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
