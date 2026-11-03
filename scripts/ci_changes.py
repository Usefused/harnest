"""Select core and extension checks within the single branch-validation workflow."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


_EXTENSION_ROOTS = tuple(
    f"official-extensions/{name}/" for name in ("docker", "hatchet", "rag")
)
_EXTENSION_COORDINATORS = frozenset({
    ".github/workflows/ci.yml",
    ".github/workflows/publish-extensions.yml",
    ".github/workflows/validate-extensions.yml",
    "scripts/ci_changes.py",
})


def affected_checks(paths: Iterable[str]) -> dict[str, bool]:
    """Preserve extension-only isolation while allowing mixed changes in one run."""

    paths = tuple(paths)
    return {
        "core": any(not path.startswith("official-extensions/") for path in paths),
        "extensions": any(
            path.startswith(_EXTENSION_ROOTS) or path in _EXTENSION_COORDINATORS
            for path in paths
        ),
    }


def _commit_sha(value: object) -> str:
    """Reject arbitrary revision expressions before invoking Git on event data."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("CI event must contain full commit SHAs")
    return value


def changed_paths(event_name: str, event: Mapping[str, Any]) -> list[str] | None:
    """Read the event's exact diff, returning None only for deliberate full checks."""

    # Manual runs have no change baseline and should exercise both sets of checks.
    if event_name == "workflow_dispatch":
        return None
    # A PR diff must exclude changes made only on its base branch after branching.
    if event_name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        separator = "..."
    elif event_name == "push":
        base, head = event["before"], event["after"]
        separator = ".."
    else:
        raise ValueError(f"unsupported CI event: {event_name}")
    # Revisions come from the event, never from arbitrary Git option syntax.
    base, head = _commit_sha(base), _commit_sha(head)
    # The first push of a branch has no parent snapshot to compare against.
    if base == "0" * 40:
        return None
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}{separator}{head}", "--"],
        check=True, capture_output=True, text=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main() -> None:
    """Emit job-selection outputs and fail closed if Git cannot determine changes."""

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    paths = changed_paths(os.environ["GITHUB_EVENT_NAME"], event)
    checks = {"core": True, "extensions": True} if paths is None else affected_checks(paths)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, active in checks.items():
            output.write(f"{name}={str(active).lower()}\n")


if __name__ == "__main__":
    main()
