"""Build once, then start a distinct agent, desktop, and MCP endpoint by name."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
IMAGE = "harnest-linux-desktop:2"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


def _port() -> int:
    """Reserve a free loopback port long enough to choose this instance's map."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _ports(count: int) -> tuple[int, ...]:
    """Avoid accidental reuse while choosing this agent's four listeners."""
    selected: list[int] = []
    while len(selected) < count:
        candidate = _port()
        if candidate not in selected:
            selected.append(candidate)
    return tuple(selected)


def _prepare() -> tuple[list[str], Path, Path | None]:
    """Reuse the image and select a matching Harnest runtime."""
    image = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Architecture}}", IMAGE],
        capture_output=True, text=True, check=False,
    )
    if image.returncode != 0 or image.stdout.strip() != "amd64":
        # Google Chrome's Linux package is amd64-only; Docker emulates it on
        # Apple Silicon so every provisioned instance runs the same image.
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", IMAGE, str(ROOT / "desktop")],
            check=True,
        )
    checkout = ROOT.parents[1]
    development_python = checkout / ".venv" / "bin" / "python"
    if (checkout / "go.mod").is_file() and development_python.exists():
        # A source checkout can have an older installed release CLI. Run the
        # checked-out Go command against its matching editable Python runtime.
        # Install this example's own SDKs there before the application resources
        # start, so a fresh checkout also has Jev and desktop dependencies.
        subprocess.run(
            ["uv", "pip", "install", "--python", str(development_python),
             "-r", str(ROOT / "pyproject.toml")],
            check=True,
        )
        return (["go", "run", "./cmd/harnest", "--python", str(development_python)],
                development_python, checkout)
    subprocess.run(["harnest", "env", "sync", str(ROOT)], check=True)
    interpreter = ROOT / ".venv" / "bin" / "python"
    if not interpreter.exists():
        raise RuntimeError("harnest env sync did not create the project's .venv")
    return ["harnest"], interpreter, None


def _wait_for_agent(process: subprocess.Popen, port: int) -> None:
    """Wait until lifecycle startup has created the desktop and Chrome."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"agent exited during startup with status {process.returncode}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.5)
    raise TimeoutError("agent did not become ready within 180 seconds")


def _wait_for_mcp(process: subprocess.Popen, port: int) -> None:
    """Prove the MCP listener is bound before advertising its URL."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MCP server exited during startup with status {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError("MCP server did not bind within 30 seconds")


def _stop(process: subprocess.Popen | None) -> None:
    """Allow normal lifecycle cleanup, then terminate a stuck subprocess."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _instance_project(port: int) -> Path:
    """Give each agent its own card with the actual listening URL."""
    target = Path(tempfile.mkdtemp(prefix="harnest-desktop-agent-"))
    try:
        shutil.copytree(ROOT, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".venv", ".harnest"))
        card = target / "agent-card.yaml"
        authored = card.read_text(encoding="utf-8")
        placeholder = "http://127.0.0.1:1907"
        if authored.count(placeholder) != 1:
            raise ValueError("agent card must contain exactly one placeholder URL")
        card.write_text(authored.replace(placeholder, f"http://127.0.0.1:{port}"), encoding="utf-8")
        return target
    except BaseException:
        shutil.rmtree(target)
        raise


def _agent_environment(name: str, api_port: int, viewer_port: int | None) -> dict[str, str]:
    """Pass one container identity and private API token to the agent runtime."""
    if not os.environ.get("OPENAI_MODEL") or not os.environ.get("OPENAI_BASE_URL"):
        raise ValueError("export OPENAI_MODEL and OPENAI_BASE_URL before provisioning")
    environment = {
        **os.environ,
        "HARNEST_DESKTOP_INSTANCE": name,
        "HARNEST_DESKTOP_IMAGE": IMAGE,
        "HARNEST_DESKTOP_API_PORT": str(api_port),
        "HARNEST_DESKTOP_TOKEN": secrets.token_urlsafe(32),
    }
    environment.pop("HARNEST_DESKTOP_VIEWER_PORT", None)
    if viewer_port is not None:
        environment["HARNEST_DESKTOP_VIEWER_PORT"] = str(viewer_port)
    return environment


def provision(name: str, *, viewer: bool = False) -> None:
    """Keep both listeners alive until interrupted or either process exits."""
    if not NAME.fullmatch(name):
        raise ValueError("name must start with a lowercase letter and use only lowercase letters, digits, or hyphens")
    cli, interpreter, checkout = _prepare()
    chosen = _ports(4 if viewer else 3)
    agent_port, mcp_port, api_port = chosen[:3]
    viewer_port = chosen[3] if viewer else None
    environment = _agent_environment(name, api_port, viewer_port)
    agent: subprocess.Popen | None = None
    mcp: subprocess.Popen | None = None
    instance_project: Path | None = None
    try:
        # The source card has a sample URL; each copied project advertises the
        # live port without mutating the shared example used by other agents.
        instance_project = _instance_project(agent_port)
        agent = subprocess.Popen(
            [*cli, "serve", str(instance_project), "--port", str(agent_port)],
            env=environment,
            cwd=checkout,
        )
        _wait_for_agent(agent, agent_port)
        mcp = subprocess.Popen(
            [str(interpreter), str(ROOT / "mcp_server.py"), "--agent-url",
             f"http://127.0.0.1:{agent_port}", "--port", str(mcp_port)],
            env=environment,
        )
        _wait_for_mcp(mcp, mcp_port)
        print(f"Agent {name}: http://127.0.0.1:{agent_port}/", flush=True)
        print(f"MCP:        http://127.0.0.1:{mcp_port}/mcp", flush=True)
        if viewer_port is not None:
            print(f"Desktop:    http://127.0.0.1:{viewer_port}/vnc.html", flush=True)
        print("Press Ctrl+C to stop this agent and its desktop.", flush=True)
        while agent.poll() is None and mcp.poll() is None:
            time.sleep(0.5)
        raise RuntimeError("agent or MCP server stopped unexpectedly")
    except KeyboardInterrupt:
        pass
    finally:
        _stop(mcp)
        _stop(agent)
        # A hard-killed runtime cannot execute its resource finalizer. Remove
        # only the container whose name belongs to this provision command.
        subprocess.run(["docker", "rm", "-f", f"harnest-desktop-{name}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if instance_project is not None:
            shutil.rmtree(instance_project)


def main() -> int:
    """Provide one command per independent agent instance."""
    parser = argparse.ArgumentParser(description="Run one Linux desktop agent and HTTP MCP endpoint")
    parser.add_argument("name", help="unique lowercase instance name")
    parser.add_argument("--viewer", action="store_true", help="expose a loopback noVNC viewer")
    arguments = parser.parse_args()
    try:
        provision(arguments.name, viewer=arguments.viewer)
    except (OSError, subprocess.CalledProcessError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"provision failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
