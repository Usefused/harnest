"""Capability palette mapped to Harnest commands and native authoring files."""

from __future__ import annotations

import json
import keyword

from fastapi import HTTPException

from .files import name

# CLI-owned scaffolds retain Harnest's framework, ownership, and collision rules.
COMMANDS = {"tool", "subagent", "task", "lifecycle", "context", "mcp", "extension"}
CATALOG = [
    ("agent", "Agent / graph", "Core", "Edit the root agent, model, instructions, or graph topology.", "agent.py"),
    ("instructions", "Instructions", "Core", "Give your agent its purpose, boundaries, and voice.", "instructions.md"),
    ("config", "Project configuration", "Core", "Framework, model environment, scaling, server, and deployment.", "config.yaml"),
    ("card", "Agent card", "Core", "Public identity, A2A discovery, interfaces, and capabilities.", "agent-card.yaml"),
    ("dependencies", "Dependencies", "Core", "Manage Python dependencies, then sync the environment.", "pyproject.toml"),
    ("tool", "Agent tool", "Capabilities", "A discovered Python function the model can call.", ""),
    ("subagent", "Subagent", "Capabilities", "A managed ADK agent for delegated work.", ""),
    ("node", "Graph agent", "Capabilities", "An explicit agent node for an ADK or LangGraph workflow.", ""),
    ("mcp", "MCP connection", "Capabilities", "Connect to an MCP server using environment credential references.", ""),
    ("skill", "Agent skill", "Capabilities", "Progressively loaded instructions in a native SKILL.md.", ""),
    ("plugin", "Agent plugin", "Capabilities", "An Agent Plugins 1.0 package for skills and MCP connections.", ""),
    ("extension", "Harnest extension", "Capabilities", "Scaffold a reusable extension through the Harnest CLI.", ""),
    ("sandbox", "Sandbox", "Capabilities", "An isolated Docker provider; install docker and assign it to an Agent.", ""),
    ("task", "Durable task", "Runtime", "Queue work with retries outside the model's tool loop.", ""),
    ("cron", "Cron schedule", "Runtime", "A five-field UTC schedule targeting a durable task.", ""),
    ("lifecycle", "Lifecycle hook", "Runtime", "Observe or customize agent execution.", ""),
    ("context", "Context provider", "Runtime", "Create an invocation-scoped shared resource.", ""),
    ("storage", "Session storage", "Runtime", "A lifecycle-owned store for sessions and checkpoints.", ""),
    ("model", "Data model", "Code", "Pydantic contracts imported through harnest.models.", ""),
    ("library", "Shared library", "Code", "Reusable helpers imported through harnest.lib.", ""),
    ("eval", "Evaluation", "Quality", "A native ADK conversation evaluation set.", ""),
    ("test", "Unit test", "Quality", "An offline pytest test run by harnest test.", ""),
    ("smoke", "Smoke test", "Quality", "An HTTP health test run by harnest test --smoke.", ""),
    ("source", "Source file", "Code", "Author any supported Harnest contract, including auth, routes, and native integrations.", ""),
]


def catalog() -> list[dict]:
    """Expose one palette contract to both the visual editor and LLM authoring context."""
    return [dict(zip(("kind", "title", "group", "description", "path"), row)) for row in CATALOG]


def resource_name(value: str) -> str:
    """Use Python-safe names because filenames become public exported symbols."""
    value = name(value).replace("-", "_")
    if keyword.iskeyword(value):
        raise HTTPException(422, "Choose a name that is not a Python keyword.")
    return value


def template(kind: str, value: str, options: dict) -> dict[str, str]:
    """Return native starter source; commands own resources that the CLI can scaffold."""
    identity = resource_name(value)
    simple = {
        "skill": (f"skills/{value}/SKILL.md", f"---\nname: {value}\ndescription: Complete {value.replace('_', ' ')} requests using verified information.\n---\n\n1. Identify the requested outcome.\n2. Use available tools when needed.\n3. Return a clear, verified result.\n"),
        "plugin": (f"plugins/{value}/plugin.json", json.dumps({"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": value}, indent=2) + "\n"),
        "model": (f"models/{identity}.py", 'from pydantic import BaseModel\n\n\nclass Result(BaseModel):\n    """Typed result shared by agent components."""\n\n    answer: str\n'),
        "library": (f"lib/{identity}.py", f'def {identity}(value: str) -> str:\n    """Normalize a shared application value."""\n    return value.strip()\n'),
        "test": (f"tests/unit/test_{identity}.py", f'def test_{identity}(agent):\n    """The compiled root exposes its public identity."""\n    assert agent.name\n'),
        "smoke": (f"tests/smoke/test_{identity}.py", f'def test_{identity}(client):\n    """The running agent reports healthy status."""\n    response = client.get("/healthz")\n    assert response.status_code == 200\n'),
        "storage": (f"lifecycle/{identity}.py", 'from harnest import lifecycle\nfrom harnest.store import MemoryStore\n\n\n@lifecycle.storage.sessions\n@lifecycle.storage.checkpoints\ndef state_store():\n    """Own one shared in-memory store for local development."""\n    return MemoryStore()\n'),
        "node": (f"subagents/{identity}.py", f'from harnest.agent import Agent\nfrom harnest.model import LiteLLMModel\n\n{identity} = Agent(\n    name={identity!r},\n    model=LiteLLMModel.from_openai_environment(),\n    instruction="Complete your assigned step and return a clear result.",\n    history="session",\n)\n'),
        "sandbox": (f"sandbox/{identity}.py", f'from harnest.extensions.docker import docker\nfrom harnest.sandbox import SandboxNetworkPolicy\n\n# Install the docker extension and assign this name in Agent(sandboxes=[...]).\n{identity} = docker.sandbox(\n    image="python:3.12-slim",\n    network_policy=SandboxNetworkPolicy.none(),\n    timeout_seconds=120,\n)\n'),
    }
    if kind in simple:
        path, text = simple[kind]
        return {path: text}
    if kind == "cron":
        task = resource_name(options.get("task", ""))
        schedule = options.get("schedule", "0 9 * * *")
        return {f"cron/{identity}.py": f'from harnest.cron import Cron\nfrom tasks.{task} import {task}\n\n{identity} = Cron(schedule={schedule!r}, task={task}, arguments={{"payload": "scheduled"}})\n'}
    if kind == "eval":
        payload = {"eval_set_id": identity, "name": value, "eval_cases": [{"evalId": "greeting", "conversation": [{"userContent": {"role": "user", "parts": [{"text": "Say hello."}]}, "finalResponse": {"role": "model", "parts": [{"text": "Hello!"}]}}]}]}
        return {f"evals/{identity}.evalset.json": json.dumps(payload, indent=2) + "\n"}
    raise HTTPException(422, "Choose a supported component or create a source file.")
