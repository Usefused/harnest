"""Exercise memory discovery and managed runtime ownership without model calls."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from harnest import context
from harnest.application import RuntimeCapabilities
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.memory import InMemoryStore
from harnest.runtime_contract import InvocationRequest, InvocationResult
from harnest.runtime_pipeline import build_runtime_pipeline

from test_neutral_runtime import FakeDriver


class MemoryDriver(FakeDriver):
    """A deterministic agent that writes only when its input requests a write."""

    def __init__(self, framework="langgraph"):
        """Select a supported managed context while preserving a stable root ID."""
        super().__init__()
        self.info = replace(self.info, framework=framework)

    async def invoke(self, request):
        """Read or explicitly write memory through the production context facade."""
        if request.input == "remember":
            await context.memory.put("style", "Concise reports")
        record = await context.memory.get("style")
        return InvocationResult(text="empty" if record is None else record.content,
                                events=(), result=record, session_id=request.session_id, metadata={})

    async def stream(self, request):
        """Exercise memory access throughout a managed streaming invocation."""
        result = await self.invoke(request)
        yield {"type": "text", "text": result.text}


def request(text, session="first", user="alice"):
    """Build a trusted invocation without accepting memory scope overrides."""
    return InvocationRequest(input=text, session_id=session, user_id=user,
                             invocation_id="inv-" + session, metadata={}, state_delta={})


class MemoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_framework_pipelines_keep_cross_session_memory_explicit(self):
        """Real lifecycle wrappers bind scope and start/close memory exactly once."""
        for framework in ("adk", "langgraph"):
            events = []
            class ObservedStore(InMemoryStore):
                async def start(self):
                    """Observe provider startup before any invocation access."""
                    events.append("start")
                async def close(self):
                    """Observe one shutdown at the outer storage boundary."""
                    events.append("close")
            store = ObservedStore()
            pipeline = build_runtime_pipeline(MemoryDriver(framework), RuntimeCapabilities(memory_store=store), ())
            try:
                self.assertEqual((await pipeline.invoke(request("read"))).text, "empty")
                await pipeline.invoke(request("remember"))
                self.assertEqual((await pipeline.invoke(request("read", "second"))).text, "Concise reports")
                self.assertEqual((await pipeline.invoke(request("read", "other", "bob"))).text, "empty")
                streamed = [item async for item in pipeline.stream(request("read", "third"))]
                self.assertEqual(streamed[0]["text"], "Concise reports")
            finally:
                await pipeline.close()
            self.assertEqual(events, ["start", "close"])


class MemoryDiscoveryTests(unittest.TestCase):
    def test_lifecycle_discovers_optional_memory_factory(self):
        """Compile storage roles from authored files, not runtime introspection."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "storage.py").write_text(
                "from harnest import lifecycle\n"
                "from harnest.checkpoint import MemoryStore\n"
                "from harnest.memory import InMemoryStore\n"
                "@lifecycle.storage.sessions\n@lifecycle.storage.checkpoints\n"
                "def sessions():\n    return MemoryStore()\n"
                "@lifecycle.storage.memory\n"
                "def memories():\n    return InMemoryStore()\n"
            )
            extensions = discover_extensions(root, framework="langgraph")
            self.assertIsInstance(extensions.storage_registry.memory, InMemoryStore)
            self.assertEqual(len(extensions.storage_registry.owned_resources()), 2)

    def test_duplicate_and_invalid_memory_factories_fail_during_discovery(self):
        """Reject ambiguous or structurally invalid custom providers before serving."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "storage.py").write_text(
                "from harnest import lifecycle\nfrom harnest.checkpoint import MemoryStore\n"
                "@lifecycle.storage.sessions\n@lifecycle.storage.checkpoints\n"
                "def sessions():\n    return MemoryStore()\n"
                "@lifecycle.storage.memory\ndef bad():\n    return object()\n"
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "MemoryStore"):
                discover_extensions(root, framework="langgraph")
            (root / "second.py").write_text(
                "from harnest import lifecycle\nfrom harnest.memory import InMemoryStore\n"
                "@lifecycle.storage.memory\ndef another():\n    return InMemoryStore()\n"
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "only one @lifecycle.storage.memory"):
                discover_extensions(root, framework="langgraph")
