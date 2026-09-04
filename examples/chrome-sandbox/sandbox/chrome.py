"""Build a pinned Playwright image only when the Chrome sandbox first runs."""

from pathlib import Path

from harnest.sandbox import Sandbox, SandboxBudget


chrome = Sandbox.container(
    docker_path=str(Path(__file__).with_name("_chrome_image")),
    network=True,
    timeout_seconds=45,
    max_output_bytes=32_768,
    budget=SandboxBudget(
        cpu=1.0,
        memory_bytes=1024 * 1024 * 1024,
        pids=128,
        scratch_bytes=256 * 1024 * 1024,
    ),
)
