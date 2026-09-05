"""Opt-in real-model/Docker probe for sandbox capabilities in authored tools.

Install ``harnest-extension-docker`` and run this file with ``OPENAI_API_KEY``,
``--base-url``, and ``--report``. Only synthetic inputs are sent to the model.
No mounts or host credentials enter containers; every container created by this
probe is recorded and removed before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import uuid

import docker
from docker.models.containers import ContainerCollection

from harnest.bundle import compile_artifact
from harnest.runtime import run_agent_message

from live_test_sandbox_models import _STORAGE, _write


_NAMES = ("calculations", "research")


def _source(root: Path, config: dict, journal: Path) -> None:
    """Create two assigned providers and a tripwire for unassigned construction."""
    prefix = f"CONFIG = {config!r}\nJOURNAL = {str(journal)!r}\n"
    _write(root / "agent.py", _AGENT)
    _write(root / "instructions.md", _INSTRUCTIONS)
    _write(root / "agent-card.yaml", "name: Named live probe\ndescription: Synthetic probe\nversion: 0.1.0\n")
    _write(root / "extensions" / "storage.py", _STORAGE)
    _write(root / "lib" / "provider.py", prefix + _PROVIDER)
    for name in (*_NAMES, "forbidden"):
        _write(root / "sandbox" / f"{name}.py", _SANDBOX.replace("NAME", repr(name)).replace("EXPORT", name))
    for name in _NAMES:
        tool = _BUSINESS_TOOL.replace("NAME", repr(name)).replace("EXPORT", name + "_digest")
        _write(root / "tools" / f"{name}_digest.py", tool)
    _write(root / "tools" / "verify_unassigned.py", _DENIAL_TOOL)


def _records(journal: Path) -> list:
    """Retain failure evidence even when the model never calls a provider."""
    return [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []


def _assert_result(records: list, response: dict, expected: str) -> dict:
    """Require independent real containers, preserved policy, and model receipts."""
    _assert_assignment(records)
    executions = [entry for entry in records if entry["event"] == "execute"]
    assert {entry["name"] for entry in executions} == set(_NAMES), executions
    assert len({entry["metadata"]["container"]["id"] for entry in executions}) == 2
    for entry in executions:
        _assert_execution(entry, response, expected)
    assert expected in response["text"], response
    return {"executions": executions, "model_output": response["text"], "unassigned_provider_constructed": False,
            "unassigned_provider_denied": True, "execution_entrypoint": "authored-business-tools"}


def _assert_assignment(records: list) -> None:
    """Require explicit denied access as well as the absence of eager factories."""
    builds = [entry["name"] for entry in records if entry["event"] == "build"]
    assert sorted(builds) == sorted(_NAMES), builds
    denials = [entry["name"] for entry in records if entry["event"] == "denied"]
    assert denials == ["forbidden"], denials
    closed = [entry["name"] for entry in records if entry["event"] == "close"]
    assert sorted(closed) == sorted(_NAMES), closed


def _assert_execution(entry: dict, response: dict, expected: str) -> None:
    """Verify each real result independently of the model's description of it."""
    output = json.loads(entry["stdout"])
    assert output["sha256"] == expected and output["platform"] == "Linux", output
    assert output["python"].startswith("3.12."), output
    assert entry["metadata"]["receipt"] in response["text"], response
    assert entry["context"]["agent_name"] == "named_sandbox_live"
    assert all(entry["context"].values()), entry


async def _framework(framework: str, config: dict, evidence: Path) -> dict:
    """Run one compiled native model loop against two actual Docker backends."""
    with tempfile.TemporaryDirectory(prefix=f"harnest-named-{framework}-") as directory:
        root = Path(directory)
        journal = root / "provider.jsonl"
        _source(root / "source", config, journal)
        compile_artifact(root / "source", root / "artifact", framework=framework)
        assert not journal.exists(), "compile eagerly constructed a provider"
        nonce = "synthetic-named-sandbox-" + uuid.uuid4().hex
        prompt = (
            "Call calculations_digest and research_digest once each with this exact nonce: "
            + nonce + ". Also call verify_unassigned once. Then report the sha256, BOTH unique "
            "provider receipts from result metadata, and whether forbidden access was denied. "
            "Never calculate or invent a receipt yourself."
        )
        try:
            response = await run_agent_message(root / "artifact", prompt, request_timeout=100)
        finally:
            records = _records(journal)
            _write(evidence, json.dumps(records, indent=2) + "\n")
        return _assert_result(records, response, hashlib.sha256(nonce.encode()).hexdigest())


def _remove_owned(client, owned: list[str]) -> list[str]:
    """Delete and verify exact owned IDs, never sweep unrelated Docker resources."""
    removed = []
    for container_id in owned:
        try:
            client.containers.get(container_id).remove(force=True, v=True)
        except docker.errors.NotFound:
            pass
        try:
            client.containers.get(container_id)
        except docker.errors.NotFound:
            removed.append(container_id)
    assert set(removed) == set(owned), "test-owned Docker resources remain"
    return removed


def _run(args, config, report) -> int:
    """Persist independent framework outcomes while guaranteeing owned cleanup."""
    owned = []
    native_run = ContainerCollection.run

    def record_run(collection, *positional, **kwargs):
        """Observe native allocations without substituting their real execution."""
        container = native_run(collection, *positional, **kwargs)
        owned.append(container.id)
        report["owned_container_ids"] = list(owned)
        _write(args.report, json.dumps(report, indent=2) + "\n")
        return container

    client = docker.DockerClient(base_url=args.base_url, timeout=5)
    try:
        client.images.get(args.image)
        with patch.object(ContainerCollection, "run", record_run):
            _run_frameworks(args, config, report)
    finally:
        report["removed_container_ids"] = _remove_owned(client, owned)
        client.close()
        _write(args.report, json.dumps(report, indent=2) + "\n")
    return int(any(result["status"] != "passed" for result in report["frameworks"].values()))


def _run_frameworks(args, config, report) -> None:
    """Continue to the other framework after a failure for useful diagnostics."""
    for framework in ("adk", "langgraph"):
        evidence = args.report.with_suffix(f".{framework}.json")
        try:
            result = asyncio.run(_framework(framework, config, evidence))
            report["frameworks"][framework] = {"status": "passed", **result}
        except Exception as error:
            report["frameworks"][framework] = {"status": "failed", "error_type": type(error).__name__}
        report["frameworks"][framework]["evidence"] = str(evidence)
        _write(args.report, json.dumps(report, indent=2) + "\n")
        print(f"named sandbox {framework}: {report['frameworks'][framework]['status']}", flush=True)


def main() -> int:
    """Keep paid calls explicit and credentials out of all evidence files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required and will never be written to reports")
    config = {"image": args.image, "base_url": args.base_url}
    report = {"model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "frameworks": {}}
    return _run(args, config, report)


_AGENT = '''import os
from harnest import Agent, LiteLLMModel

root_agent = Agent(
    name="named_sandbox_live", sandboxes=["calculations", "research"],
    model=LiteLLMModel(
        model="openai/" + os.getenv("OPENAI_MODEL", "gpt-4.1-mini").removeprefix("openai/"),
        api_base=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        temperature=0, max_tokens=1200, timeout=35, max_retries=0,
    ),
)
'''

_INSTRUCTIONS = '''You are running a synthetic integration probe. Call the authored
calculations_digest and research_digest business tools exactly once each with
the user's nonce. They own their execution implementation; do not produce or
request arbitrary Python. Also call verify_unassigned once to check access denial.
Never simulate execution. Return the sha256, both provider-generated receipts
from metadata exactly, and the access denial outcome.
'''

_SANDBOX = '''from harnest import Sandbox
from harnest.lib.provider import build

EXPORT = Sandbox.provider(
    lambda: build(NAME), name=NAME, timeout_seconds=10,
    metadata={"assigned_name": NAME, "region": "local", "tenant": "synthetic",
              "properties": {"framework_neutral": True, "types": [None, False, 0, 1.25, "λ"]}},
)
'''

_BUSINESS_TOOL = '''from harnest import context, tool
from harnest.sandbox_types import sandbox_metadata_to_dict


@tool
async def EXPORT(nonce: str) -> dict:
    """Calculate a synthetic digest using application-owned code and policy."""
    code = (
        "import hashlib, json, platform\\n"
        f"print(json.dumps({{'sha256': hashlib.sha256({nonce!r}.encode()).hexdigest(), "
        "'platform': platform.system(), 'python': platform.python_version()}))"
    )
    result = await context.sandboxes[NAME].aexecute(code)
    return {"stdout": result.stdout, "stderr": result.stderr,
            "metadata": sandbox_metadata_to_dict(result.metadata)}
'''

_DENIAL_TOOL = '''from harnest import context, tool
from harnest.context import ContextResourceError
from harnest.lib.provider import record


@tool
def verify_unassigned() -> dict:
    """Check that authored code cannot execute an unassigned sandbox provider."""
    try:
        context.sandboxes["forbidden"].execute("print('forbidden')")
    except ContextResourceError:
        record({"event": "denied", "name": "forbidden"})
        return {"forbidden_access": "denied"}
    raise AssertionError("unassigned sandbox access was permitted")
'''

_PROVIDER = '''
import hashlib
import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
import time
import uuid
from harnest import Sandbox, SandboxResult
from harnest_extension_docker.extension import docker_sandbox
from harnest.sandbox_types import sandbox_metadata_to_dict


def record(value):
    """Persist synthetic evidence without any transport credentials."""
    with Path(JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\\n")


class ReportingBackend:
    """Wrap actual native Docker with independently observable provider metadata."""

    def __init__(self, name):
        """Keep each assigned provider's native backend independently owned."""
        self.name = name
        self.calls = 0
        self.backend = docker_sandbox(
            **CONFIG, timeout_seconds=10, max_output_bytes=65536,
            options={"error_retry_attempts": 1, "stateful": False,
                     "optimize_data_file": False,
                     "code_block_delimiters": [("```python\\n", "\\n```")],
                     "execution_result_delimiters": ("<result>", "</result>")},
        ).build()._backend

    def close(self):
        """Release the owned native backend when the managed application closes."""
        self.backend.close()
        record({"event": "close", "name": self.name})

    def execute(self, request):
        """Run synthetic Python and expose observed identity, policy, and image facts."""
        self.calls += 1
        assert self.calls <= 2, "model exceeded the bounded execution budget"
        assert request.metadata["assigned_name"] == self.name
        assert request.metadata["properties"]["framework_neutral"] is True
        started = time.monotonic()
        result = self.backend.execute(request)
        assert not result.stderr, result.stderr
        native = self.backend._executor
        container = native._container
        container.reload()
        host = container.attrs["HostConfig"]
        metadata = {
            "receipt": self.name + "-" + uuid.uuid4().hex,
            "provider": "adk-native-docker", "assigned_name": self.name,
            "versions": {key: version(key) for key in ("google-adk", "docker")},
            "container": {
                "id": container.id, "image_id": container.image.id,
                "architecture": container.image.attrs["Architecture"],
                "network_mode": host["NetworkMode"],
                "network_disabled": container.attrs["Config"]["NetworkDisabled"],
                "memory": host["Memory"], "nano_cpus": host["NanoCpus"],
                "cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                "mounts": len(container.attrs["Mounts"]),
                "running_after_success": container.attrs["State"]["Running"],
            },
            "options": native.model_dump(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "request_metadata": sandbox_metadata_to_dict(request.metadata),
        }
        assert not container.attrs["Mounts"]
        assert not container.attrs["State"]["Running"]
        assert host["Memory"] == host["NanoCpus"] == 0
        assert container.attrs["Config"]["NetworkDisabled"] is True
        record({"event": "execute", "name": self.name, "stdout": result.stdout,
                "stderr": result.stderr, "metadata": metadata, "context": asdict(request.context),
                "code_sha256": hashlib.sha256(request.code.encode()).hexdigest()})
        return SandboxResult(stdout=result.stdout, stderr=result.stderr, metadata=metadata)


def build(name):
    """Detect accidental eager loading or exposure of an unassigned provider."""
    record({"event": "build", "name": name})
    assert name != "forbidden", "unassigned provider was constructed"
    return ReportingBackend(name)
'''


if __name__ == "__main__":
    raise SystemExit(main())
