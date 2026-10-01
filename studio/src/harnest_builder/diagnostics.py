"""Read-only Python diagnostics for unsaved Studio editor buffers."""

import json
import os
import subprocess
import sys

import yaml

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .files import LIMIT, source_path


class Draft(BaseModel):
    """Bound the draft independently of the saved file's syntax and revision."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    path: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=LIMIT)


def check_python(path: str, text: str, target: str = "py311") -> dict:
    """Lint stdin without importing project code, writing files, or applying fixes."""
    if len(text.encode()) > LIMIT:
        raise HTTPException(413, "Source files are limited to 1 MiB.")
    # Isolated Python and Ruff ignore project-local modules and configuration.
    # Correctness diagnostics stay predictable across agents and never execute them.
    argv = [sys.executable, "-I", "-m", "ruff", "check", "--isolated", "--no-cache",
            "--select", "E4,E7,E9,F", "--target-version", target, "--output-format", "json",
            "--stdin-filename", path, "-"]
    try:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("RUFF_")}
        result = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=5, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(503, "Python linting is unavailable. Try again shortly.") from error
    if result.returncode not in (0, 1):
        raise HTTPException(503, "Python linting is unavailable. Check the Studio Ruff installation.")
    return _diagnostics(result.stdout)


def _diagnostics(output: str) -> dict:
    """Expose only bounded messages and locations, excluding fix payloads and host paths."""
    try:
        items = json.loads(output)
        diagnostics = [{"code": item["code"] or "syntax-error", "message": item["message"],
                        "start": item["location"], "end": item["end_location"],
                        "severity": "error" if item["code"] in (None, "invalid-syntax", "F821", "F822", "F823") else "warning"}
                       for item in items[:200]]
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(503, "Python linting returned an invalid response.") from error
    return {"diagnostics": diagnostics, "truncated": len(items) > 200}


def _python_target(root) -> str:
    """Honor the agent's declared runtime instead of rejecting newer Python syntax."""
    try:
        config = yaml.safe_load((root / "config.yaml").read_text())
        version = str(config.get("spec", {}).get("runtime", {}).get("version", "3.11"))
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return "py311"
    return "py" + version.replace(".", "") if version in {"3.11", "3.12", "3.13", "3.14"} else "py311"


def install_routes(app, workspace) -> None:
    """Use the existing authenticated project boundary for transient draft analysis."""

    @app.post("/api/diagnostics")
    def diagnostics(body: Draft):
        """Validate ownership before reading a draft; the saved source is never changed."""
        root = workspace.project(body.project)
        path = source_path(root, body.path)
        if path.suffix != ".py":
            return {"diagnostics": [], "truncated": False}
        return check_python(body.path, body.text, _python_target(root))
