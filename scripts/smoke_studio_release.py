"""Exercise the shipped CLI and Studio bundle without source paths or a model provider."""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def request(url: str, token: str = ""):
    """Use loopback directly even when CI or the developer has an HTTP proxy configured."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(urllib.request.Request(url, headers={"Authorization": "Bearer " + token}), timeout=3)


def ready(process, log: Path, port: int) -> str:
    """Wait through first-run dependency setup and fail with the captured launch log."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and process.poll() is None:
        match = re.search(r"#token=([^\s]+)", log.read_text(errors="replace"))
        if match:
            try:
                with request(f"http://127.0.0.1:{port}/api/workspace", match[1]):
                    return match[1]
            except (OSError, urllib.error.URLError):
                pass
        time.sleep(.2)
    raise RuntimeError("Studio release did not start:\n" + log.read_text(errors="replace")[-10000:])


def stop(process) -> None:
    """Give the native CLI time to stop its Python server and any supervised descendants."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.terminate()
    process.wait(timeout=25)


def launch(cli: Path, cwd: Path, workspace: Path, environment: dict, explicit: bool) -> None:
    """Check default/explicit workspace selection, authentication, and every shipped UI module."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    arguments = [str(cli), "studio", "--port", str(port)]
    if explicit:
        arguments.extend(["--workspace", str(workspace)])
    log = cwd / "studio-smoke.log"
    with log.open("w") as output:
        process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdout=output, stderr=subprocess.STDOUT)
        try:
            token = ready(process, log, port)
            origin = f"http://127.0.0.1:{port}"
            with request(origin + "/api/workspace", token) as response:
                result = json.load(response)
            assert Path(result["path"]).resolve() == workspace.resolve(), result
            for path in ["/", "/assets/app.js", "/assets/canvas.js", "/assets/ui.js", "/assets/style.css", "/assets/deployment.js"]:
                with request(origin + path) as response:
                    assert response.status == 200 and response.read(), path
            try:
                request(origin + "/api/jobs")
            except urllib.error.HTTPError as error:
                assert error.code == 401, error
            else:
                raise AssertionError("Studio accepted an unauthenticated API request")
        finally:
            stop(process)
    if explicit:
        assert "Preparing the bundled Studio runtime" not in log.read_text(), "cached launch reinstalled Studio"


def compiled_server(environment: dict, cwd: Path) -> None:
    """Start the packaged assistant with no compilation or model call and check authenticated readiness."""
    root = Path(environment["HARNEST_RUNTIME_DIR"]).parent / "studio"
    state = json.loads((root / "environment.json").read_text())
    python = root / state["directory"] / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = """
import asyncio
from harnest_builder.assistant_server import AssistantServer
from harnest_builder.assistant_build import PACKAGE
assert not (PACKAGE / 'assistant_source').exists()
async def check():
    server = AssistantServer()
    try:
        await server.ensure_running('openai/studio-packaging-test')
        assert server.temporary is None, 'production assistant compiled on first use'
        assert (await server.client.get('/sessions')).status_code == 200
    finally:
        await server.close()
asyncio.run(check())
"""
    subprocess.run([str(python), "-I", "-c", script], cwd=cwd, env=environment, check=True, timeout=90)


def main() -> None:
    """Run from a temporary empty workspace with no editable packages or inherited Python overrides."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="harnest-studio-release-") as directory:
        root = Path(directory)
        workspace = root / "workspace with spaces"
        override = root / "selected workspace"
        workspace.mkdir()
        override.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("HARNEST_") and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        environment["HARNEST_RUNTIME_DIR"] = str(root / "runtime")
        launch(options.cli.resolve(), workspace, workspace, environment, False)
        launch(options.cli.resolve(), workspace, override, environment, True)
        compiled_server(environment, workspace)
    print("Studio release passed: default/explicit workspace, bundled UI, API authentication, cached runtime, compiled assistant.")


if __name__ == "__main__":
    main()
