from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from harnest.bundle import compile_artifact
import harnest.extensions as extension_namespace
from harnest.extensions import ExtensionContextUnavailableError
from harnest.runtime import create_fastapi_app


class HarnestExtensionADKLiveTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, contents: str) -> None:
        """Write one isolated authored source file for the compiled fixture."""

        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")

    def _agent(self, root: Path) -> None:
        """Author a deterministic ADK model and portable persistence boundary."""

        self._write(
            root,
            "agent.py",
            '''
            from google.adk.models import BaseLlm, LlmResponse
            from google.genai import types
            from harnest.agent import Agent

            class DeterministicLlm(BaseLlm):
                async def generate_content_async(self, llm_request, stream=False):
                    del llm_request, stream
                    yield LlmResponse(content=types.Content(
                        role="model",
                        parts=[types.Part(text="runtime extension smoke ok")],
                    ))

            root_agent = Agent(
                name="runtime_extension_smoke",
                description="Deterministic runtime extension smoke agent.",
                model=DeterministicLlm(model="deterministic-local"),
                instruction="Return the deterministic local response.",
            )
            ''',
        )
        self._write(
            root,
            "agent-card.yaml",
            '''
            name: Harnest Extension Smoke
            description: Deterministic Harnest Extension lifecycle smoke.
            version: 0.1.0
            ''',
        )
        self._write(root, "instructions.md", "Return the deterministic response.\n")
        self._write(
            root,
            "lifecycle/storage.py",
            '''
            from harnest.checkpoint import MemoryStore
            from harnest import lifecycle
            from harnest.session import InMemorySessionStore

            @lifecycle.storage.sessions
            def sessions():
                return InMemorySessionStore()

            @lifecycle.storage.checkpoints
            def checkpoints():
                return MemoryStore()
            ''',
        )

    def _extension(self, root: Path) -> None:
        """Author one extension with application and invocation lifecycle proof."""

        self._write(
            root,
            "extensions/liveextension/extension.yaml",
            '''
            apiVersion: harnest.dev/v1alpha1
            kind: Extension
            metadata:
              name: liveextension
              version: 1.0.0
            runtime:
              entrypoint: extension:extension
            contributes:
              lifecycle: [lifecycle/]
            capabilities:
              - lifecycle.agent
            ''',
        )
        self._write(
            root,
            "extensions/liveextension/extension.py",
            '''
            import json
            import os
            from pathlib import Path
            import sys
            from harnest import context
            from harnest.extensions import Extension, ExtensionContext

            def _record(event, **fields):
                target = os.environ.get("HARNEST_EXTENSION_LIVE_EVENTS")
                if target is None:
                    return
                with Path(target).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"event": event, **fields}) + "\\n")

            class LiveContext(ExtensionContext):
                def snapshot(self):
                    self._require_active()
                    return {
                        "agent": context.agent_name,
                        "depth": context.depth,
                        "isRoot": context.is_root,
                    }

            class LiveExtension(Extension[LiveContext]):
                async def start(self, start_context):
                    _record(
                        "start",
                        extension=start_context.extension_name,
                        namespaceLoaded="harnest.extensions.liveextension" in sys.modules,
                    )

                async def stop(self):
                    _record(
                        "stop",
                        namespaceLoaded="harnest.extensions.liveextension" in sys.modules,
                    )

                def create_context(self, base):
                    return LiveContext(base.extension_name)

                def record(self, event, **fields):
                    _record(event, **fields)

            extension = LiveExtension()
            ''',
        )
        self._write(
            root,
            "extensions/liveextension/lifecycle/invocation.py",
            '''
            from harnest import context
            from harnest import lifecycle
            from harnest.extension_runtime_context import extension_mutation
            from harnest.extensions.liveextension import LiveContext, extension

            async def _record(phase):
                direct = extension.context
                facade = context.extensions("liveextension", LiveContext)
                async with extension_mutation(
                    "liveextension", "record_lifecycle", trigger="agent"
                ):
                    extension.record(
                        phase,
                        sameContext=direct is facade,
                        direct=direct.snapshot(),
                        facade=facade.snapshot(),
                    )

            @lifecycle.agent.before
            async def before(lifecycle_context, request):
                del request
                await _record("before")
                return lifecycle_context.next()

            @lifecycle.agent.after
            async def after(lifecycle_context, result):
                del result
                await _record("after")
                return lifecycle_context.next()
            ''',
        )

    @staticmethod
    def _events(path: Path) -> list[dict[str, object]]:
        """Read only the bounded evidence emitted by this isolated fixture."""

        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_compiled_adk_http_runtime_owns_extension_context_and_namespace(self):
        """Cross compiler, FastAPI, ADK, extension context, and shutdown boundaries."""

        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "agent", root / "artifact"
            events = root / "events.jsonl"
            self._agent(source)
            self._extension(source)
            compile_artifact(source, artifact, framework="adk")
            self.assertFalse(hasattr(extension_namespace, "liveextension"))

            with patch.dict(
                os.environ,
                {"HARNEST_EXTENSION_LIVE_EVENTS": str(events)},
            ):
                app = create_fastapi_app(
                    artifact,
                    bind_host="testserver",
                    playground_enabled=False,
                )
                self.assertTrue(hasattr(extension_namespace, "liveextension"))
                extension = extension_namespace.liveextension.extension
                with TestClient(app) as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)
                    created = client.post(
                        "/sessions", json={"id": "extension-smoke", "state": {}}
                    )
                    response = client.post(
                        "/responses",
                        json={
                            "input": "prove extension context",
                            "sessionId": "extension-smoke",
                        },
                    )
                    self.assertEqual(created.status_code, 201)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json()["outputText"], "runtime extension smoke ok"
                    )
                    with self.assertRaises(ExtensionContextUnavailableError):
                        _ = extension.context

                records = self._events(events)
                self.assertEqual(
                    [item["event"] for item in records],
                    ["start", "before", "after", "stop"],
                )
                self.assertTrue(records[0]["namespaceLoaded"])
                self.assertTrue(records[-1]["namespaceLoaded"])
                for record in records[1:3]:
                    self.assertTrue(record["sameContext"])
                    self.assertEqual(record["direct"], record["facade"])

            self.assertFalse(hasattr(extension_namespace, "liveextension"))
            self.assertNotIn("harnest.extensions.liveextension", sys.modules)


if __name__ == "__main__":
    unittest.main()
