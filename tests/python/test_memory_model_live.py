"""Opt-in real model tool calls through compiled agents and persistent memory."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app
from test_memory_agent import write_agent


_TOOLS = '''from harnest import context
from harnest.agent import tool
import json
import os
from pathlib import Path

def evidence(operation, result):
    """Retain synthetic test evidence, never provider requests or credentials."""
    with Path(os.environ['HARNEST_MEMORY_TEST_EVIDENCE']).open('a') as stream:
        stream.write(json.dumps({'operation': operation, 'result': result}) + '\\n')
    return result

@tool
async def remember(key: str, content: str) -> dict:
    """Save a memory only when the user explicitly asks to remember it."""
    record = await context.memory.put(key, content)
    return evidence('remember', {'key': record.key, 'content': record.content})

@tool
async def recall(key: str) -> dict:
    """Read a saved memory by key. Use this for every recall request."""
    record = await context.memory.get(key)
    return evidence('recall', {'content': record.content if record else 'EMPTY'})

@tool
async def find_memory(query: str) -> dict:
    """Search saved memory using literal case-sensitive text."""
    page = await context.memory.search(query)
    return evidence('search', {'contents': [record.content for record in page.items]})

@tool
async def forget(key: str) -> dict:
    """Delete one saved memory when the user explicitly asks to forget it."""
    return evidence('forget', {'deleted': await context.memory.delete(key)})
'''


def write_model_agent(root, provider):
    """Replace deterministic graph logic with a configured model and real tools."""
    write_agent(root, provider)
    name = 'memory_model_' + uuid.uuid4().hex
    (root / 'agent.py').write_text(
        'import json, os\nfrom harnest.agent import Agent\nfrom harnest.model import LiteLLMModel\n'
        f'root_agent = Agent(name={name!r}, model=LiteLLMModel.from_openai_environment('
        'temperature=0, max_tokens=512, timeout=45, num_retries=0, '
        '**json.loads(os.environ.get("HARNEST_TEST_MODEL_OPTIONS", "{}"))), '
        'instruction="Use the appropriate memory tool for every request. Never invent saved data. '
        'Save only on an explicit remember request. Copy tool content exactly in the final answer.")\n'
    )
    (root / 'tools').mkdir()
    (root / 'lib' / 'memory_tools.py').write_text(_TOOLS)
    for name in ('remember', 'recall', 'find_memory', 'forget'):
        (root / 'tools' / f'{name}.py').write_text(f'from harnest.lib.memory_tools import {name}\n')


@unittest.skipUnless(os.environ.get('HARNEST_TEST_LIVE_MODEL') == '1', 'requires explicit live-model opt-in')
class MemoryModelLiveTests(unittest.TestCase):
    def request(self, client, prompt, evidence, operation):
        """Require actual tool evidence as well as the model's HTTP response."""
        before = len(evidence.read_text().splitlines()) if evidence.exists() else 0
        session = client.post('/sessions', json={})
        self.assertEqual(session.status_code, 201)
        result = client.post('/responses', json={'input': prompt, 'sessionId': session.json()['id']})
        self.assertEqual(result.status_code, 200, result.text)
        events = [json.loads(line) for line in evidence.read_text().splitlines()[before:]]
        matches = [event['result'] for event in events if event['operation'] == operation]
        self.assertTrue(matches, f'model did not invoke {operation}')
        return result.json()['outputText'], matches[-1]

    def exercise(self, provider):
        """Prove write, restart recall, search and deletion on both model adapters."""
        for framework in ('adk', 'langgraph'):
            with self.subTest(provider=provider, framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, artifact, evidence = root / 'source', root / 'artifact', root / 'evidence.jsonl'
                write_model_agent(source, provider)
                secret = 'synthetic-memory-' + uuid.uuid4().hex
                with patch.dict(os.environ, {'HARNEST_MEMORY_TEST_EVIDENCE': str(evidence)}):
                    compile_artifact(source, artifact, framework=framework)
                    with TestClient(create_fastapi_app(artifact)) as client:
                        _, saved = self.request(client, f'Remember this memory. The key is exactly "code". The content is exactly "{secret}".', evidence, 'remember')
                        self.assertEqual(saved['key'], 'code')
                        self.assertEqual(saved['content'], secret)
                    self.recover_and_forget(artifact, evidence, secret)

    def recover_and_forget(self, artifact, evidence, secret):
        """Use fresh sessions without the secret in their prompts after restart."""
        with TestClient(create_fastapi_app(artifact)) as client:
            text, recalled = self.request(client, 'Recall the saved memory whose key is exactly "code".', evidence, 'recall')
            self.assertEqual(recalled['content'], secret)
            self.assertIn(secret, text)
            _, found = self.request(client, 'Search memories for the literal text synthetic-memory.', evidence, 'search')
            self.assertEqual(found['contents'], [secret])
            _, removed = self.request(client, 'Forget the saved memory whose key is exactly "code".', evidence, 'forget')
            self.assertTrue(removed['deleted'])
            _, missing = self.request(client, 'Recall the saved memory whose key is exactly "code".', evidence, 'recall')
            self.assertEqual(missing['content'], 'EMPTY')

    @unittest.skipUnless(os.environ.get('HARNEST_TEST_POSTGRES_DSN'), 'requires PostgreSQL')
    def test_postgres_model_tool_journey(self):
        """Run the actual model against PostgreSQL-backed compiled agents."""
        self.exercise('postgres')

    @unittest.skipUnless(os.environ.get('HARNEST_TEST_REDIS_URL'), 'requires Redis')
    def test_redis_model_tool_journey(self):
        """Run the actual model against Redis-backed compiled agents."""
        self.exercise('redis')
