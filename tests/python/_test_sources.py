"""Shared authored-file discovery for repository source-contract checks."""

import os
from pathlib import Path


def authored_files(root: Path, pattern: str) -> list[Path]:
    """Exclude local environments and generated output before descending into examples."""
    result = []
    for directory, folders, files in os.walk(root):
        # Running an example in place must not turn cached dependencies into authored source.
        folders[:] = [name for name in folders if not name.startswith(".")
                      and name not in {"__pycache__", "node_modules", "build", "dist"}]
        result.extend(Path(directory) / name for name in files if Path(name).match(pattern))
    return sorted(result)
