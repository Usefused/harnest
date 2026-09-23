"""Select compiled source content without inferring arbitrary Python file reads."""
from __future__ import annotations

from pathlib import Path

from .compile_selection import FILENAME


_PROJECT_FILES = frozenset({
    "config.yaml", "server.yaml", "agent-card.yaml", "pyproject.toml",
    "harnest.lock", "harnest-runtime.lock", FILENAME,
})


def compile_source_paths(root: Path) -> frozenset[Path]:
    """Keep runtime code and conventional capabilities without copying project documentation."""
    scopes = _agent_scopes(root)
    trees = []
    for scope in scopes:
        # Installed capabilities are package boundaries: preserve their assets and
        # portable executables together rather than guessing which files they read.
        trees.extend(_capability_trees(scope))
    selected = {
        path.relative_to(root) for path in root.rglob("*")
        if _included_file(path, root, scopes, trees)
    }
    selected.add(_dependency_file(root))
    return frozenset(selected)


def _capability_trees(scope: Path) -> list[Path]:
    """Retain installed packages rather than the parent folder's guides and samples."""
    result = []
    for name in ("skills", "plugins", "extensions"):
        for child in (scope / name).glob("*"):
            if child.is_dir() and not child.name.startswith((".", "_")):
                result.append(child)
    return result


def _agent_scopes(root: Path) -> frozenset[Path]:
    """Follow only the nested-subagent convention when retaining implicit instructions."""
    scopes = {root}
    pending = [root]
    while pending:
        directory = pending.pop() / "subagents"
        if directory.is_symlink():
            continue
        for child in directory.glob("*/agent.py"):
            if child.parent.name.startswith((".", "_")) or child.parent.is_symlink():
                continue
            scopes.add(child.parent)
            pending.append(child.parent)
    return frozenset(scopes)


def _included_file(path: Path, root: Path, scopes: frozenset[Path], trees: list[Path]) -> bool:
    """Apply inclusion rules independently of the stricter shared exclusion policy."""
    if path.suffix in {".py", ".pyi"}:
        return True
    if path.parent == root and path.name in _PROJECT_FILES:
        return True
    if path.name == "instructions.md" and path.parent in scopes:
        return True
    if _evaluation_file(path, scopes):
        return True
    return any(path == tree or tree in path.parents for tree in trees)


def _evaluation_file(path: Path, scopes: frozenset[Path]) -> bool:
    """Keep conventional evaluation declarations available to compiled-agent tooling."""
    return (
        path.parent.name == "evals" and path.parent.parent in scopes
        and (path.name == "test_config.json" or path.name.endswith(".evalset.json"))
    )


def _dependency_file(root: Path) -> Path:
    """Retain configured Python metadata needed for dependency inspection after copying."""
    import yaml
    from .server_config import _UniqueKeyLoader

    config = root / "config.yaml"
    if not config.is_file():
        return Path("pyproject.toml")
    settings = yaml.load(config.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return Path(settings.get("spec", {}).get("runtime", {}).get("dependencyFile", "pyproject.toml"))
