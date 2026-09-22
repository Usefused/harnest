#!/usr/bin/env python3
"""Build dependency-free native agent launchers before embedding release assets."""

from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Cross-build the launcher matrix with the same Go toolchain as the CLI."""
    destination = ROOT / "internal" / "agentlauncher" / "assets"
    destination.mkdir(parents=True, exist_ok=True)
    for system in ("darwin", "linux", "windows"):
        for architecture in ("amd64", "arm64"):
            environment = dict(os.environ, GOOS=system, GOARCH=architecture, CGO_ENABLED="0")
            subprocess.run(
                ["go", "build", "-trimpath", "-ldflags=-s -w", "-o",
                 str(destination / f"agent_{system}_{architecture}"), "./cmd/harnest-agent"],
                cwd=ROOT, env=environment, check=True,
            )


if __name__ == "__main__":
    main()
