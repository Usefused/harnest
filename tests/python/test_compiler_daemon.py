from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from unittest import mock

from harnest import compiler_daemon


class CompilerDaemonTests(unittest.TestCase):
    def test_module_entrypoint_keeps_protocol_alive_after_a_bad_request(self):
        completed = subprocess.run(
            [sys.executable, "-m", "harnest.compiler_daemon"],
            input="[]\n{}\n",
            text=True,
            capture_output=True,
            check=True,
        )

        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(not item["ok"] for item in responses))

    def test_reuses_process_for_serial_compiles_and_isolates_authored_stdout(self):
        requests = [
            {
                "id": "1",
                "source": "/agent",
                "output": "/artifact/one",
                "entrypoint": "agent:root_agent",
                "framework": "adk",
                "mode": "managed",
                "cliEnabled": False,
            },
            {
                "id": "2",
                "source": "/agent",
                "output": "/artifact/two",
                "entrypoint": "agent:root_agent",
                "framework": "adk",
                "mode": "managed",
                "cliEnabled": True,
            },
        ]
        input_stream = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        output_stream = io.StringIO()

        def compile_side_effect(*args, **kwargs):
            print("authored diagnostic")
            return {"digest": f"digest-{kwargs['cli_enabled']}"}

        with mock.patch.object(
            compiler_daemon, "compile_artifact", side_effect=compile_side_effect
        ) as compile_mock, mock.patch.object(compiler_daemon.sys, "stderr", io.StringIO()):
            compiler_daemon.serve(input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(
            responses,
            [
                {"id": "1", "ok": True, "digest": "digest-False"},
                {"id": "2", "ok": True, "digest": "digest-True"},
            ],
        )
        self.assertEqual(compile_mock.call_count, 2)

    def test_reports_malformed_and_compile_errors_without_ending_stream(self):
        input_stream = io.StringIO(
            "[]\n"
            + json.dumps(
                {
                    "id": "next",
                    "source": "/agent",
                    "output": "/artifact",
                    "entrypoint": "agent:root_agent",
                    "framework": "adk",
                    "mode": "managed",
                }
            )
            + "\n"
        )
        output_stream = io.StringIO()
        with mock.patch.object(
            compiler_daemon,
            "compile_artifact",
            side_effect=RuntimeError("invalid graph"),
        ):
            compiler_daemon.serve(input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], "")
        self.assertFalse(responses[0]["ok"])
        self.assertIn("JSON object", responses[0]["error"])
        self.assertEqual(responses[1]["id"], "next")
        self.assertFalse(responses[1]["ok"])
        self.assertIn("invalid graph", responses[1]["error"])


if __name__ == "__main__":
    unittest.main()
