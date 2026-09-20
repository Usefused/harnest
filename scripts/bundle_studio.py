"""Stage Studio's server, browser assets, and compiled assistant into the release wheel."""

import importlib.util
import json
from pathlib import Path
import shutil


def bundle_studio(root: Path, staged: Path, version: str) -> None:
    """Compile against staged release sources and pin the assistant's tested framework."""
    package = staged / "src" / "harnest_builder"
    shutil.copytree(root / "agent-builder/src/harnest_builder", package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_assistant"))
    builder = _builder(root)
    # The compiler reads installed distribution metadata. A private metadata entry
    # ensures snapshot builds identify the staged release, not the build host's SDK.
    metadata = staged / "src" / f"harnest-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.4\nName: harnest\nVersion: {version}\n")
    try:
        builder.compile_assistant(package / "_assistant", runtime_source=staged / "src")
    finally:
        shutil.rmtree(metadata)
    shutil.rmtree(package / "assistant_source")
    manifest = json.loads((package / "_assistant/harnest-manifest.json").read_text())
    if manifest["harnestVersion"] != version:
        raise ValueError("Studio assistant was compiled with a different Harnest release")
    project = staged / "pyproject.toml"
    text = project.read_text()
    requirement = "google-adk==" + manifest["framework"]["version"]
    text = text.replace('studio = ["google-adk>=2.8,<3"]', "studio = " + json.dumps([requirement]))
    text += '\n[tool.hatch.build.targets.wheel.force-include]\n"src/harnest_builder" = "harnest_builder"\n'
    project.write_text(text)


def _builder(root: Path):
    """Load the existing compiler entrypoint without importing the Studio web application."""
    path = root / "agent-builder/src/harnest_builder/assistant_build.py"
    spec = importlib.util.spec_from_file_location("harnest_studio_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
