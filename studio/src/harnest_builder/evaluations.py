"""Read evaluation authoring presets from the installed CLI's canonical catalog."""

import json
import subprocess

from fastapi import HTTPException


def presets(cli, root) -> list[dict]:
    """Read fixed CLI metadata with a deadline and keep subprocess diagnostics local."""
    try:
        command = (cli,) if isinstance(cli, str) else tuple(cli)
        result = subprocess.run([*command, "add", "eval", "--list-metrics"], cwd=root, capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise HTTPException(503, "Evaluation presets are unavailable. Restart Studio with an updated Harnest CLI.") from error
