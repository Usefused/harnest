#!/usr/bin/env python3
"""Build the embedded runtime wheel with an explicit, verified version."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the project compatibility dependency.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = re.compile(r'(?m)^(version = ")[^"]+("\s*)$')
SOURCE_VERSION = re.compile(r'(?m)^(_SOURCE_VERSION = ")[^"]+(".*)$')


def parse_arguments() -> argparse.Namespace:
    """Parse the release version and isolated output boundary."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument("--allow-version-override", action="store_true")
    parser.add_argument("--no-isolation", action="store_true")
    return parser.parse_args()


def source_versions(root: Path) -> tuple[str, str]:
    """Read both checked-in version sources without importing Harnest."""

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    compatibility = (root / "src/harnest/compatibility.py").read_text(
        encoding="utf-8"
    )
    match = SOURCE_VERSION.search(compatibility)
    if match is None:
        raise ValueError("compatibility.py does not declare _SOURCE_VERSION")
    source = re.search(r'_SOURCE_VERSION = "([^"]+)"', match.group(0))
    if source is None:
        raise ValueError("compatibility.py has an invalid _SOURCE_VERSION")
    return project["project"]["version"], source.group(1)


def replace_version(pattern: re.Pattern[str], text: str, version: str) -> str:
    """Replace exactly one annotated version declaration."""

    updated, count = pattern.subn(rf"\g<1>{version}\g<2>", text, count=1)
    if count != 1:
        raise ValueError("expected exactly one version declaration")
    return updated


def stage_source(root: Path, destination: Path, version: str) -> None:
    """Stage only wheel inputs so snapshot overrides never dirty the checkout."""

    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    (destination / "pyproject.toml").write_text(
        replace_version(PROJECT_VERSION, project, version), encoding="utf-8"
    )
    shutil.copy2(root / "README.md", destination / "README.md")
    shutil.copytree(root / "src", destination / "src")
    compatibility_path = destination / "src/harnest/compatibility.py"
    compatibility = compatibility_path.read_text(encoding="utf-8")
    compatibility_path.write_text(
        replace_version(SOURCE_VERSION, compatibility, version), encoding="utf-8"
    )


def build_wheel(arguments: argparse.Namespace) -> None:
    """Validate release sources, then build from a disposable staged tree."""

    project_version, source_version = source_versions(ROOT)
    if project_version != source_version:
        raise ValueError(
            "pyproject.toml and compatibility.py versions must match before packaging"
        )
    if arguments.version != project_version and not arguments.allow_version_override:
        raise ValueError(
            f"requested wheel {arguments.version} does not match source {project_version}"
        )

    arguments.outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="harnest-wheel-") as temporary:
        staged = Path(temporary)
        # Snapshot names are GoReleaser state, so they belong only in disposable
        # build inputs; real releases must already carry their reviewed version.
        stage_source(ROOT, staged, arguments.version)
        command = [sys.executable, "-m", "build", "--wheel"]
        if arguments.no_isolation:
            command.append("--no-isolation")
        command.extend(["--outdir", str(arguments.outdir), str(staged)])
        subprocess.run(command, check=True)


def main() -> int:
    """Build the wheel and report policy failures without a traceback."""

    try:
        build_wheel(parse_arguments())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"build runtime wheel: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
