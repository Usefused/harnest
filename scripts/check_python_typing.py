"""Require discoverable signatures for every shipped Harnest function and method."""

from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path
import sys
import tomllib


def signature_gaps(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Check positional, keyword-only, variadic, and return annotations together."""
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
    gaps = [arg.arg for arg in arguments if arg.arg not in {"self", "cls"} and arg.annotation is None]
    if node.returns is None:
        gaps.append("return")
    return gaps


def audit(root: Path, *, exclude: tuple[str, ...] = ()) -> tuple[int, int, list[str]]:
    """Walk source directly so private, nested, and optional-backend methods count."""
    classes = functions = 0
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(fnmatchcase(path.relative_to(root).as_posix(), pattern) for pattern in exclude):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes += 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions += 1
                missing = signature_gaps(node)
                if missing:
                    failures.append(f"{path}:{node.lineno} {node.name}: {', '.join(missing)}")
    return classes, functions, failures


def package_exclusions(project: Path, package: Path) -> tuple[str, ...]:
    """Use wheel exclusions so retired local prototypes cannot inflate coverage."""
    with (project / "pyproject.toml").open("rb") as stream:
        wheel = tomllib.load(stream)["tool"]["hatch"]["build"]["targets"]["wheel"]
    prefix = package.relative_to(project).as_posix() + "/"
    return tuple(pattern.removeprefix(prefix) for pattern in wheel.get("exclude", ())
                 if pattern.startswith(prefix))


def main() -> int:
    """Fail the release gate when any SDK signature loses its IDE-visible types."""
    root = Path(__file__).resolve().parents[1] / "src/harnest"
    classes, functions, failures = audit(root, exclude=package_exclusions(root.parent.parent, root))
    for failure in failures:
        print(failure)
    print(f"Typing coverage: {classes} classes, {functions} functions/methods, {len(failures)} incomplete signatures")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
