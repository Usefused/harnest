"""Plain-language explanations for filesystem authoring mistakes."""

from pathlib import Path


_FOLDER_CONTENTS = {
    "tools": "Python (.py) files, with one Agent Tool per file",
    "tasks": "Python (.py) files, with one declared task per file",
    "cron": "Python (.py) files, with one Cron schedule per file",
    "mcp": "Python (.py) files, each defining a client() function that returns an MCPClient",
    "sandbox": "Python (.py) files, each defining one Sandbox variable matching its filename; agents explicitly assign permitted names with sandboxes=[...]",
    "subagents": "Python (.py) files or named subfolders containing agent.py",
    "skills": "one subfolder per skill, each containing an uppercase SKILL.md file",
    "plugins": "one subfolder per plugin, rather than loose files",
    "evals": "evaluation files ending in .evalset.json and an optional test_config.json, directly in this folder",
}


def authoring_guidance(problem: str, *, expected: str, fix: str) -> str:
    """Keep the precise diagnosis while adding the rule and an actionable repair."""
    return f"{problem}\n\nWhat Harnest expects: {expected}.\nHow to fix: {fix}"


def inactive_entry_hint(path: Path) -> str:
    """Suggest a reversible opt-out only at boundaries that ignore underscores."""
    return (
        f"If {path.name!r} is only a note, backup, or unused example, rename it "
        f"to {'_' + path.name!r} or move it outside this folder. "
        "A name starting with _ is left out of automatic discovery; "
        "renaming it does not turn it into a working feature."
    )


def folder_entry_error(problem: str, path: Path, *, kind: str) -> str:
    """Explain why an ordinary file becomes a configuration error in a feature folder."""
    expected = _FOLDER_CONTENTS[kind]
    return authoring_guidance(
        problem,
        expected=f"{kind}/ contains {expected}",
        fix=(
            "If this is an active feature, use the file or folder layout described above. "
            + inactive_entry_hint(path)
        ),
    )
