"""Build and launch portable agents on each native CI host, without a model provider."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


AGENT = '''from pathlib import Path
import os
import subprocess
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from harnest.agent import Agent

class LocalModel(BaseLlm):
    async def generate_content_async(self, request, stream=False):
        """Exercise packaged resources and a real Python console entrypoint offline."""
        tool = subprocess.check_output(["uvicorn", "--version"], text=True)
        assert "uvicorn" in tool.lower(), tool
        resource = Path(__file__).with_name("instructions.md").read_text()
        text = "portable:" + os.environ["PACK_LABEL"] + ":" + resource
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))

root_agent = Agent(name="{name}", model=LocalModel(model="local-test"), instruction="Run the local test.")
'''


def invoke(arguments, *, cwd: Path, environment: dict, expected: str = "") -> str:
    """Capture diagnostics while requiring the expected successful runtime behavior."""
    result = subprocess.run(list(map(str, arguments)), cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
    if result.returncode or expected not in result.stdout:
        raise AssertionError(f"Command failed ({result.returncode}): {arguments}\n{result.stdout}")
    return result.stdout


def create_agent(root: Path, name: str, dependency: str) -> Path:
    """Give equivalent dependency closures distinct declared-input identities."""
    directory = root / name
    directory.mkdir()
    create_storage(directory)
    (directory / "agent-card.yaml").write_text(f"""name: {name}
description: Offline packaging test.
version: 1.0.0
supportedInterfaces:
  - url: http://localhost:1907/a2a
    protocolBinding: JSONRPC
    protocolVersion: '1.0'
capabilities:
  streaming: true
defaultInputModes: [text/plain]
defaultOutputModes: [text/plain]
skills:
  - id: echo
    name: Echo
    description: Offline response.
    tags: [test]
""", encoding="utf-8")
    (directory / "agent.py").write_text(AGENT.format(name=name), encoding="utf-8")
    (directory / "instructions.md").write_text("bundled-resource", encoding="utf-8")
    (directory / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\nversion="0.1.0"\n'
        f'requires-python=">=3.12,<3.13"\ndependencies=["{dependency}"]\n', encoding="utf-8")
    (directory / "config.yaml").write_text(f'''apiVersion: harnest.dev/v1alpha1
kind: Agent
metadata:
  name: {name}
spec:
  entrypoint: agent:root_agent
  environment:
    PACK_LABEL: {name}
  framework:
    name: adk
    mode: managed
  interfaces:
    cli: true
  runtime:
    dependencyFile: pyproject.toml
    version: '3.12'
''', encoding="utf-8")
    return directory


def create_storage(directory: Path) -> None:
    """Provide explicit ephemeral session and checkpoint stores for the fixture."""
    lifecycle = directory / "lifecycle"
    lifecycle.mkdir()
    (lifecycle / "storage.py").write_text('''from harnest import lifecycle
from harnest.store import MemoryStore

store = MemoryStore()

@lifecycle.storage.sessions
def session_store():
    """Keep this smoke agent independent of external databases."""
    return store

@lifecycle.storage.checkpoints
def checkpointer():
    """Use the same authority for committed sessions and private checkpoints."""
    return store
''', encoding="utf-8")


def build_artifacts(cli: Path, root: Path, wheel: Path | None) -> Path:
    """Compile attached and embedded executables with shared build-time objects."""
    environment = dict(os.environ, HARNEST_AGENT_CACHE=str(root / "build cache"))
    sales = create_agent(root, "sales", "packaging>=24")
    support = create_agent(root, "support", "packaging>=24,<100")
    dist = root / "dist"
    dist.mkdir()
    extra = ["--wheel", wheel] if wheel else []
    for name, owners in [("shared", [sales, support]), ("alternate", [sales])]:
        arguments = [cli, "runtime", "build", "--output", dist / name]
        for owner in owners:
            arguments.extend(["--agent", owner])
        invoke(arguments + extra, cwd=root, environment=environment)
    for name, source, pack, embed in [
        ("sales", sales, "shared", False), ("support", support, "shared", False),
        ("embedded", sales, "shared", True), ("alternate", sales, "alternate", False),
    ]:
        args = [cli, "compile", source, "--runtime", dist / pack, "--output", dist / f"{name}.exe"]
        invoke(args + (["--embed-runtime"] if embed else []), cwd=root, environment=environment)
    shutil.rmtree(root / "build cache")
    # No execution may depend on the authored source or the CLI working directory.
    shutil.rmtree(sales)
    shutil.rmtree(support)
    moved = root / "moved deployment café"
    dist.rename(moved)
    return moved


def manifest(pack: Path) -> dict:
    """Read the published inventory used to verify physical file sharing."""
    return json.loads((pack / "runtime-manifest.json").read_text())


def check_deduplication(dist: Path, cache: Path) -> None:
    """Prove hardlink identity across packs and materialized trees, not byte equality."""
    first, second = manifest(dist / "shared"), manifest(dist / "alternate")
    assert first["digest"] != second["digest"], "fixture did not create independent runtime identities"
    objects = {item["object"] for item in first["files"] if item.get("object")}
    common = objects & {item["object"] for item in second["files"] if item.get("object")}
    assert len(common) > 100, "fixture did not exercise a real dependency closure"
    for identity in common:
        source = dist / "shared" / "objects" / identity
        assert source.samefile(dist / "alternate" / "objects" / identity), identity
        assert source.samefile(cache / "objects" / identity), identity
    check_tree_links(first, cache)
    print(f"Verified {len(common)} physically shared objects across runtime packs and cache.")


def check_tree_links(inventory: dict, cache: Path) -> None:
    """Require materialized dependencies to reuse the deployment object store."""
    for item in inventory["files"]:
        if item.get("object"):
            path = cache / "runtimes" / inventory["digest"] / item["path"]
            assert path.samefile(cache / "objects" / item["object"]), item["path"]


def deployment_environment(root: Path) -> dict:
    """Remove host interpreters and Python overrides without losing Windows system variables."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("PYTHON") and key.upper() not in {"PATH", "VIRTUAL_ENV"}}
    empty = root / "empty path"
    empty.mkdir()
    environment.update(PATH=str(empty), HARNEST_AGENT_CACHE=str(root / "launch cache"))
    return environment


def check_failures(dist: Path, root: Path, environment: dict) -> None:
    """Require missing and mismatched attachments to fail before importing the agent."""
    for arguments, expected in [
        (["run", "hello"], "attach runtime"),
        (["--runtime", dist / "alternate", "run", "hello"], "does not match required"),
    ]:
        result = subprocess.run([str(dist / "sales.exe"), *map(str, arguments)], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=60)
        assert result.returncode != 0 and expected in result.stderr, result
    with zipfile.ZipFile(dist / "sales.exe") as archive:
        assert not any(name.startswith("runtime/") for name in archive.namelist())


def ensure_windows_console() -> None:
    """Give headless Windows runners a console for a targeted Ctrl+Break test."""
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        # ERROR_ACCESS_DENIED means this process already has a console.
        if not kernel.AllocConsole() and ctypes.get_last_error() != 5:
            raise ctypes.WinError(ctypes.get_last_error())


def ready(process, port: int, log: Path) -> None:
    """Wait for a real HTTP response and surface early process failures."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with opener.open(f"http://127.0.0.1:{port}/agent", timeout=1) as response:
                assert response.status == 200
                return
        except (OSError, urllib.error.URLError):
            time.sleep(.2)
    raise AssertionError("Agent server did not start:\n" + log.read_text(errors="replace"))


def check_server(dist: Path, root: Path, environment: dict) -> None:
    """Exercise HTTP startup, console termination and cleanup on the native host."""
    ensure_windows_console()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log = root / "server.log"
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    with log.open("w") as output:
        process = subprocess.Popen([str(dist / "sales.exe"), "--runtime", str(dist / "shared"),
                                    "serve", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT, **flags)
        try:
            ready(process, port, log)
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            process.wait(timeout=30)
            assert "Application shutdown complete" in log.read_text(), log.read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", port)) != 0, "server outlived launcher"


def exercise(cli: Path, root: Path, wheel: Path | None) -> None:
    """Run genuine agents after relocation with no host Python in PATH."""
    dist = build_artifacts(cli, root, wheel)
    environment = deployment_environment(root)
    check_failures(dist, root, environment)
    for name, pack, label in [("sales", "shared", "sales"), ("support", "shared", "support"),
                              ("alternate", "alternate", "sales")]:
        invoke([dist / f"{name}.exe", "--runtime", dist / pack, "run", "hello with spaces Ω"],
               cwd=root, environment=environment, expected=f"portable:{label}:bundled-resource")
    check_deduplication(dist, Path(environment["HARNEST_AGENT_CACHE"]))
    check_server(dist, root, environment)
    # A fresh cache and hidden packs prove the executable contains its whole runtime.
    (dist / "shared").rename(dist / "hidden-shared")
    (dist / "alternate").rename(dist / "hidden-alternate")
    environment["HARNEST_AGENT_CACHE"] = str(root / "embedded cache")
    invoke([dist / "embedded.exe", "run", "hello"], cwd=root, environment=environment,
           expected="portable:sales:bundled-resource")
    print("Attached and embedded executable smoke tests passed.")


def main() -> None:
    """Keep all fixtures outside the source checkout, including paths with spaces."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    options = parser.parse_args()
    wheel = options.wheel.resolve() if options.wheel else None
    with tempfile.TemporaryDirectory(prefix="harnest executable ") as directory:
        exercise(options.cli.resolve(), Path(directory), wheel)


if __name__ == "__main__":
    main()
