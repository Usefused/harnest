"""Build the private authoring agent with the release's canonical skill sources."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).parent
SKILL_NAMES = ("harnest-authoring", "harnest-authentication", "fused-admin", "fused-auth")


def skill_sources() -> Path:
    """Use repository skills in development and the captured snapshot in source releases."""
    configured = os.getenv("HARNEST_BUILDER_SKILLS")
    candidates = [Path(configured)] if configured else [
        PACKAGE.parents[2] / "cmd" / "harnest" / "authoring_skill",
        PACKAGE / "assistant_source" / "skills",
    ]
    for candidate in candidates:
        if all((candidate / name / "SKILL.md").is_file() for name in SKILL_NAMES):
            return candidate
    raise RuntimeError("Harnest authoring skills missing; set HARNEST_BUILDER_SKILLS to the canonical authoring_skill directory.")


def stage_source(destination: Path) -> None:
    """Snapshot authored source and complete skill references, excluding local build state."""
    shutil.copytree(PACKAGE / "assistant_source", destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "skills"))
    for name in SKILL_NAMES:
        shutil.copytree(skill_sources() / name, destination / "skills" / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def compile_assistant(output: Path, *, runtime_source: Path | None = None) -> Path:
    """Run Harnest's compiler at build time; never bake provider credentials into the bundle."""
    with tempfile.TemporaryDirectory(prefix="harnest-builder-compile-") as directory:
        source = Path(directory) / "agent"
        stage_source(source)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("HARNEST_BUILDER_")}
        if runtime_source is not None:
            environment["PYTHONPATH"] = str(runtime_source)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["HARNEST_BUILDER_MODEL"] = "ollama_chat/qwen3.5:cloud"
        result = subprocess.run(
            [sys.executable, "-m", "harnest.cli", "compile", str(source), "--output", str(output), "--framework", "adk", "--mode", "managed"],
            env=environment, capture_output=True, text=True, timeout=180, check=False,
        )
        if result.returncode:
            raise RuntimeError("Compiling the Studio agent failed:\n" + result.stderr[-6000:])
    return output
