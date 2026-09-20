"""Compiled Studio agent grounding, private HTTP transport, and lifecycle integration."""

import asyncio
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-builder" / "src"))
from harnest_builder.app import create_app
from harnest_builder.assistant_build import compile_assistant, skill_sources
from harnest_builder.assistant_server import AssistantServer, final_reply, traceback_summary
from harnest_builder.prompting import _payload
from harnest_builder.assistant_errors import failure_category


class Provider(BaseHTTPRequestHandler):
    """Implement a deterministic OpenAI-compatible transport behind the actual agent runtime."""

    requests = []
    failure_status = None
    unknown_tool = False

    def do_POST(self):
        """Return an inert source proposal and retain the model request for grounding assertions."""
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(payload)
        if self.failure_status:
            self.send_response(self.failure_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"private-provider-message-and-key","type":"authentication_error"}}')
            return
        content = json.dumps({"summary": "Clearer instructions", "files": [{"path": "instructions.md", "text": "Answer clearly.\n"}]})
        result = {"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                  "model": "test", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if self.unknown_tool and (len(self.requests) == 1 or self.unknown_tool == "always"):
            result["choices"] = [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "missing-tool", "type": "function", "function": {"name": "read_files", "arguments": '{"paths":["lifecycle/storage.py"]}'}}]}}]
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keep test prompts and authorization headers out of test logs."""


class AgentBuilderAssistantTests(unittest.IsolatedAsyncioTestCase):
    """Use real compiled artifacts and native HTTP, without requiring external model access."""

    @classmethod
    def setUpClass(cls):
        """Compile the same authored service and skills that the production wheel carries."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.artifact = compile_assistant(Path(cls.temp.name) / "compiled")

    async def asyncSetUp(self):
        """Provide a private deterministic provider and fresh supervising server for every test."""
        Provider.requests = []
        Provider.failure_status = None
        Provider.unknown_tool = False
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        self.enterContext(patch.dict(os.environ, {
            "HARNEST_BUILDER_API_BASE": f"http://127.0.0.1:{self.provider.server_port}/v1",
            "HARNEST_BUILDER_API_KEY": "test-only-key",
            "HARNEST_BUILDER_ARTIFACT": str(self.artifact),
        }))
        self.server = AssistantServer(self.artifact)
        self.addAsyncCleanup(self.server.close)

    def test_compiled_guidance_matches_canonical_release_skills(self):
        """Do not replace shipped user guidance with a separately maintained system prompt."""
        for source in skill_sources().rglob("*.md"):
            target = self.artifact / "source" / "skills" / source.relative_to(skill_sources())
            self.assertEqual(target.read_bytes(), source.read_bytes())
        manifest = json.loads((self.artifact / "harnest-manifest.json").read_text())
        self.assertEqual(manifest["framework"]["name"], "adk")

    async def test_private_server_and_proposal_route_preserve_review_boundary(self):
        """Exercise Studio -> native Harnest -> model -> validated proposal without source writes."""
        root = Path(self.temp.name) / "workspace"
        project = root / "sample"
        project.mkdir(parents=True, exist_ok=True)
        (project / "config.yaml").write_text("metadata:\n  name: sample\n")
        (project / "instructions.md").write_text("Be useful.\n")
        app = create_app(root, "/missing/harnest", token="test-studio")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://127.0.0.1", headers={"Authorization": "Bearer test-studio"}) as client:
                response = await client.post("/api/propose", json={"project": "sample", "prompt": "Improve instructions", "model": "openai/test", "paths": ["instructions.md"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["files"][0]["before"], "Be useful.\n")
            self.assertEqual((project / "instructions.md").read_text(), "Be useful.\n")
            runtime = app.state.assistant
            rejected = await runtime.client.post("/sessions", json={}, headers={"Authorization": ""})
            self.assertEqual(rejected.status_code, 401)
            self.assertEqual((await runtime.client.get("/sessions")).json()["sessions"], [])
            process = runtime.process
            request = json.dumps(Provider.requests)
            self.assertIn("harnest-authoring", request)
            self.assertIn("Improve instructions", request)
            self.assertNotIn("test-only-key", request)
        self.assertIsNotNone(process.returncode)

    async def test_model_change_restarts_only_the_owned_runtime(self):
        """A model selection cannot race with a running request or reuse the prior model."""
        await self.server.ensure_running("openai/first")
        first = self.server.process
        await self.server.ensure_running("openai/first")
        self.assertIs(self.server.process, first)
        await self.server.ensure_running("openai/second")
        self.assertIsNot(self.server.process, first)
        self.assertIsNotNone(first.returncode)
        self.assertEqual(self.server.model, "openai/second")

    async def test_packaged_artifact_never_compiles_at_runtime(self):
        """Production requires its precompiled artifact and does not silently fall back to sources."""
        with patch("harnest_builder.assistant_server.compile_assistant", side_effect=AssertionError("unexpected compile")):
            await self.server.prepare()
            missing = AssistantServer(Path(self.temp.name) / "missing-artifact")
            with self.assertRaisesRegex(Exception, "compiled Studio agent is missing"):
                await missing.prepare()

    async def test_failed_native_response_deletes_the_session(self):
        """Do not retain source in session history after model or transport failures."""
        calls = []

        def transport(request):
            """Simulate a failed invocation after successful native session creation."""
            calls.append((request.method, request.url.path))
            code = 502 if request.url.path == "/responses" else 200
            return httpx.Response(code, json={})

        self.server.client = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(transport))
        with self.assertRaises(httpx.HTTPStatusError):
            await self.server.invoke("private source")
        self.assertEqual(calls[-1][0], "DELETE")
        self.assertTrue(calls[-1][1].startswith("/sessions/"))

    async def test_cancelled_compilation_finishes_before_cleanup(self):
        """Cancellation must not delete a directory while its compiler thread is still writing."""
        started, finish = threading.Event(), threading.Event()

        def compile_wait(_path):
            """Hold a deterministic compiler thread until the cancellation assertion completes."""
            started.set()
            finish.wait(5)

        with patch("harnest_builder.assistant_server.compile_assistant", compile_wait):
            task = asyncio.create_task(self.server.compile_development())
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with suppress(asyncio.CancelledError):
                await task

    async def test_native_provider_auth_failure_is_specific_without_leaking_details(self):
        """Exercise the compiled model hook through real HTTP and suppress raw provider errors."""
        Provider.failure_status = 401
        messages = [{"role": "user", "content": "{}"}, {"role": "user", "content": "private-source-prompt"}]
        with self.assertLogs("harnest_builder.assistant_server", level="WARNING") as captured:
            with self.assertRaises(Exception) as caught:
                await self.server.completion(model="openai/test", messages=messages)
        detail = getattr(caught.exception, "detail", "")
        self.assertIn("provider rejected its credentials", detail)
        self.assertIn("Reference:", detail)
        self.assertIn("category=authentication", " ".join(captured.output))
        for private in ("private-provider-message-and-key", "private-source-prompt", "test-only-key"):
            self.assertNotIn(private, detail + " ".join(captured.output))
        self.assertTrue(all("retry=False" in line for line in captured.output))
        self.assertIsNone(self.server.client)
        self.assertIsNone(self.server.process)

    async def test_unknown_source_tool_is_corrected_inside_the_native_agent(self):
        """The exact live missing-tool failure becomes feedback and a completed proposal."""
        Provider.unknown_tool = True
        response = await self.server.completion(model="openai/test", messages=[
            {"role": "user", "content": "{}"}, {"role": "user", "content": "Switch back to inmemory"}])
        self.assertIn("instructions.md", response.choices[0].message.content)
        self.assertEqual(len(Provider.requests), 2)
        feedback = [message for message in Provider.requests[-1]["messages"] if message.get("role") == "tool"]
        self.assertIn("NOT a callable tool", json.dumps(feedback))
        self.assertEqual((await self.server.client.get("/sessions")).json()["sessions"], [])

    async def test_repeated_unknown_tools_stop_after_two_corrections(self):
        """A provider that ignores corrective feedback cannot create an unbounded tool loop."""
        Provider.unknown_tool = "always"
        with self.assertRaises(Exception) as caught:
            await self.server.completion(model="openai/test", messages=[
                {"role": "user", "content": "{}"}, {"role": "user", "content": "Switch storage"}])
        self.assertIn("Reference:", caught.exception.detail)
        self.assertEqual(len(Provider.requests), 3)
        self.assertIsNone(self.server.process)

    async def test_transient_transport_retry_uses_fresh_server_and_is_bounded(self):
        """A read-only proposal retries once, while a second failure remains visible."""
        with patch.object(self.server, "ensure_running", new_callable=AsyncMock), patch.object(self.server, "stop", new_callable=AsyncMock) as stop:
            with patch.object(self.server, "invoke", new_callable=AsyncMock) as invoke:
                invoke.side_effect = [httpx.ReadTimeout("private details"), "proposal"]
                self.assertEqual(await self.server.request("model", "private source"), "proposal")
                self.assertEqual(invoke.await_count, 2)
                stop.assert_awaited_once()
                invoke.reset_mock()
                invoke.side_effect = httpx.ReadTimeout("private details")
                with self.assertRaises(Exception) as caught:
                    await self.server.request("model", "private source")
                self.assertIn("timed out", caught.exception.detail)
                self.assertNotIn("private", caught.exception.detail)
                self.assertEqual(invoke.await_count, 2)


class AssistantResponseTests(unittest.TestCase):
    """Structured native response extraction must not treat tool narration as a proposal."""

    def test_trace_metadata_excludes_source_and_exception_messages(self):
        """Debug references must never retain exception payloads or full local file paths."""
        text = '  File "/private/path/functions.py", line 1329, in _get_tool\n    secret = "private-code"\nValueError: private-provider-message\n'
        self.assertEqual(traceback_summary(text), {"exceptions": ["ValueError"], "frames": ["functions.py:1329:_get_tool"]})

    def test_failure_categories_ignore_stale_or_untrusted_error_text(self):
        """Session identity and fixed categories prevent stale failures or messages from leaking."""
        response = httpx.Response(500, request=httpx.Request("POST", "http://localhost/responses"), text="private details")
        error = httpx.HTTPStatusError("private details", request=response.request, response=response)
        self.assertEqual(failure_category(error, {"session": "old", "category": "timeout"}, "current"), "runtime_error")
        self.assertEqual(failure_category(error, {"session": "current", "category": "authentication"}, "current"), "authentication")
        self.assertEqual(failure_category(error, {"session": "current", "category": ["private details"]}, "current"), "runtime_error")

    def test_final_message_excludes_intermediate_skill_text(self):
        """The concatenated outputText is unsuitable when skill loading emits narration."""
        reply = '{"summary":"Ready","files":[]}'
        result = {"outputText": "Loading a skill." + reply, "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Loading a skill."}]},
            {"type": "tool_result", "output": {"text": "Untrusted source"}},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply}]},
        ]}
        self.assertEqual(final_reply(result), reply)


    def test_final_model_turn_keeps_chunks_separated_by_metadata(self):
        """Metadata between text events must not truncate a JSON object."""
        result = {"outputText": "ignored", "output": [
            {"type": "tool_result", "output": {}},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"files":'}]},
            {"type": "agent_metadata"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '[]}' }]},
        ]}
        self.assertEqual(final_reply(result), '{"files":[]}')

    def test_one_fenced_proposal_after_skill_narration_is_accepted(self):
        """Accept the live provider's final envelope without parsing prose as source instructions."""
        self.assertEqual(_payload('Loaded guidance.\n\n```json\n{"files": []}\n```'), {"files": []})
        self.assertEqual(_payload('{"read_files": ["agent.py"]}'), {"read_files": ["agent.py"]})

    def test_ambiguous_or_malformed_proposals_remain_rejected(self):
        """Never choose silently between multiple code blocks or reinterpret non-JSON text."""
        for reply in ('```json\n{"files": []}\n```\n```json\n{"files": []}\n```',
                      '```python\nprint("hello")\n```', 'Narration {"files": []}',
                      '```json\nnot JSON\n```'):
            with self.subTest(reply=reply), self.assertRaises(Exception):
                _payload(reply)
