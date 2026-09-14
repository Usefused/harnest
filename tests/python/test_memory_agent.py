"""Compile and serve a fresh memory-enabled agent across real framework boundaries."""

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app
from _session_store_fixture import write_session_store


def write_agent(root: Path, provider: str) -> None:
    """Create a deterministic agent with deliberate writes and explicit storage."""
    name = "memory_agent_" + uuid.uuid4().hex
    root.mkdir()
    library = root / "lib"
    library.mkdir()
    (library / "remember.py").write_text(
        "from harnest import context\nfrom harnest.graph import Event\n"
        "async def remember(value):\n"
        "    '''Write only when the invocation explicitly asks to remember.'''\n"
        "    if value == 'remember':\n"
        "        await context.memory.put('preference', 'Concise reports')\n"
        "    if value == 'forget':\n"
        "        await context.memory.delete('preference')\n"
        "    record = await context.memory.get('preference')\n"
        "    text = 'empty' if record is None else record.content\n"
        "    return Event(output=text, message=text)\n"
    )
    (root / "agent.py").write_text(
        "from harnest.graph import START, Edge, Graph\n"
        "from harnest.lib.remember import remember\n"
        f"root_agent = Graph(name={name!r}, nodes={{'remember': remember}}, edges=(Edge(START, 'remember'),))\n"
    )
    (root / "instructions.md").write_text("Save only explicit preferences.\n")
    (root / "agent-card.yaml").write_text("name: Memory test\ndescription: Explicit memory fixture.\nversion: 0.1.0\n")
    write_session_store(root)
    providers = {
        "local": ("harnest.memory", "InMemoryStore", ""),
        "postgres": ("harnest_postgres", "PostgresMemoryStore", "os.environ['HARNEST_TEST_POSTGRES_DSN']"),
        "redis": ("harnest_redis", "RedisMemoryStore", "os.environ['HARNEST_TEST_REDIS_URL']"),
    }
    module, constructor, argument = providers[provider]
    (root / "lifecycle" / "memory.py").write_text(
        f"import os\nfrom harnest import lifecycle\nfrom {module} import {constructor}\n"
        "@lifecycle.storage.memory\ndef memory():\n"
        "    '''Construct a provider without connecting during compilation.'''\n"
        f"    return {constructor}({argument})\n"
    )


class MemoryAgentTests(unittest.TestCase):
    def respond(self, client, text):
        """Use a new HTTP session for every request to prove cross-session memory."""
        session = client.post("/sessions", json={})
        self.assertEqual(session.status_code, 201, session.text)
        response = client.post("/responses", json={"input": text, "sessionId": session.json()["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["outputText"]

    def exercise(self, provider):
        """Cross compiler, artifact loader, HTTP, framework and memory boundaries."""
        for framework in ("adk", "langgraph"):
            with self.subTest(provider=provider, framework=framework), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                root, artifact = workspace / "source", workspace / "artifact"
                write_agent(root, provider)
                compile_artifact(root, artifact, framework=framework)
                with TestClient(create_fastapi_app(artifact)) as client:
                    self.assertEqual(self.respond(client, "read"), "empty")
                    self.assertEqual(self.respond(client, "remember"), "Concise reports")
                    self.assertEqual(self.respond(client, "read"), "Concise reports")
                if provider != "local":
                    with TestClient(create_fastapi_app(artifact)) as client:
                        self.assertEqual(self.respond(client, "read"), "Concise reports")
                        self.assertEqual(self.respond(client, "forget"), "empty")

    def test_fresh_compiled_agent_uses_explicit_memory(self):
        """Run both real framework adapters without making any model calls."""
        self.exercise("local")

    @unittest.skipUnless(os.environ.get("HARNEST_TEST_POSTGRES_DSN"), "requires PostgreSQL")
    def test_postgres_agent_remembers_after_restart(self):
        """Recover authored agent memory through a new runtime and database pool."""
        self.exercise("postgres")

    @unittest.skipUnless(os.environ.get("HARNEST_TEST_REDIS_URL"), "requires Redis")
    def test_redis_agent_remembers_after_restart(self):
        """Recover authored agent memory through a new runtime and Redis client."""
        self.exercise("redis")
