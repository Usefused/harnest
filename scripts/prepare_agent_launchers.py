#!/usr/bin/env python3
"""Build dependency-free native agent launchers before embedding release assets."""

import argparse
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TARGETS = tuple(f"{system}-{architecture}" for system in ("darwin", "linux", "windows")
                for architecture in ("amd64", "arm64"))


def build_launcher(target: str, destination: Path) -> None:
    """Build one dependency-free launcher using the release's portable flags."""
    system, architecture = target.split("-")
    environment = dict(os.environ, GOOS=system, GOARCH=architecture, CGO_ENABLED="0")
    subprocess.run(
        ["go", "build", "-trimpath", "-ldflags=-s -w", "-o",
         str(destination / f"agent_{system}_{architecture}"), "./cmd/harnest-agent"],
        cwd=ROOT, env=environment, check=True,
    )


def main() -> None:
    """Build all release targets, or only the targets selected by a native smoke job."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", choices=TARGETS)
    parser.add_argument("--output-directory", type=Path,
                        default=ROOT / "internal" / "agentlauncher" / "assets")
    arguments = parser.parse_args()
    destination = arguments.output_directory.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # Packaging omits --target and still builds every architecture; smoke jobs
    # only embed their host launcher, so cross-building the rest repeats work.
    for target in arguments.target or TARGETS:
        build_launcher(target, destination)


if __name__ == "__main__":
    main()
