"""Generate docs/codebase/files.md, the one-line map of every Harnest source file.

Each entry comes from the file itself: a Python module's docstring or the
comment above a Go file's ``package`` clause. Editing that docstring or
comment is how you change the map, and a file without one fails ``--check``.

    python scripts/codebase_map.py            # rewrite docs/codebase/files.md
    python scripts/codebase_map.py --check    # fail if the map is stale
    python scripts/codebase_map.py deps bundle           # what bundle.py imports / is imported by
    python scripts/codebase_map.py deps frameworks/adk   # the same for a whole package
    python scripts/codebase_map.py graph --json out.json # full Python import graph

Only tracked and new, non-ignored files are listed, so leftovers never appear.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "codebase" / "files.md"
PACKAGE = "src/harnest/"

# Areas of the flat harnest package, in match order: the first pattern that
# matches a module path (relative to src/harnest, without ".py") wins. New
# modules land in the first matching area, or "Shared utilities".
AREAS: tuple[tuple[str, str, str], ...] = (
    ("Framework adapters",
     "Everything framework-specific. One package per framework; see docs/frameworks.md.",
     r"frameworks/.*"),
    ("Compiler",
     "Turns an agent folder into a compiled artifact: discovery, composition, lowering, artifact loader.",
     r"bundle|compile_.*|compiler_daemon|source_tree|application|application_layout|agent_tree"
     r"|_library|project_lock|compatibility|authoring_errors"),
    ("Python command line",
     "The Python half of the CLI. The Go binary in cmd/harnest runs these with `python -m`.",
     r"cli|runtime_cli|mcp_cli|channel_cli|provisioner_cli|orchestrator"),
    ("Authoring API",
     "What agent code imports: agents, graphs, models, tools, skills, structured output, UI events.",
     r"__init__|agent/.*|graph|model|structured|output|skills|skill_selection|client_tool|client_input"
     r"|_agent_tool|tool_arguments|ui|ui_output|tracing|a2a"),
    ("Lifecycle, extensions and plugins",
     "Decorator hooks, Harnest Extensions, standard Agent Plugins, and authored HTTP routes.",
     r"lifecycle.*|tool_lifecycle|extension.*|extensions/.*|_extension_project|agent_plugin.*"
     r"|dynamic_agent_plugins|plugin|http.*"),
    ("Auth and credentials",
     "Caller authentication, the Agent Runtime Principal, and credential resolution.",
     r"auth|agent_principal|credentials"),
    ("Invocation context",
     "What code can reach during one invocation through harnest.context.",
     r"context.*|_context_agent"),
    ("Tasks, cron and continuations",
     "Queued tasks, schedules, durable tools, and waits on external work.",
     r"task.*|_task_storage|runtime_task.*|cron.*|_cron_storage|runtime_cron_store|continuation"
     r"|external_continuation.*|runtime_continuation|durable"),
    ("Runtime and serving",
     "The framework-neutral runtime: driver contract, HTTP/SSE/WebSocket, A2A, AG-UI, server limits.",
     r"runtime.*|neutral_runtime|server.*|response_status"),
    ("Models and tokens",
     "Model-call lifecycle, transports, and optional token policies.",
     r"model_.*|token.*|tokens"),
    ("MCP and OpenAPI tools",
     "MCP client connections, MCP HTTP transport, OpenAPI bridges, and Fused connectors.",
     r"mcp.*|openapi_.*|fused_connectors|_fused_.*"),
    ("Storage",
     "Sessions, checkpoints, memory, and the built-in Postgres and Redis backends.",
     r"store.*|checkpoint|session|storage_registry|memory.*"),
    ("Content and media",
     "Multimodal content, asset storage, and media staging.",
     r"content.*|asset.*|assets|stored_media|transient_media|_media_annotations"),
    ("Sandboxes", "Portable isolated code execution.", r"sandbox.*"),
    ("Decisions", "Typed, bounded judgments and their providers.", r"decision.*"),
    ("Channels", "Chat-platform channels and their storage.", r"channel.*"),
    ("Testing and evaluation",
     "harnest test, the built-in evaluator, eval capture, and provider conformance suites.",
     r"testing.*|eval.*|_eval.*|evaluation"),
    ("Playground and Studio backend",
     "Development routes behind `harnest serve` and Studio.",
     r"playground.*"),
    ("Provisioning", "Docker Compose and Kubernetes deployment.", r"provisioner.*"),
    ("Upgrades", "`harnest upgrade` migrations.", r"upgrade.*"),
    ("Packs", "Harnest packs and branded CLIs (`harnest pack ...`).", r"authoring/.*"),
    ("Observability", "Logging and OpenTelemetry.", r"telemetry|logging"),
    ("Shared utilities", "Small helpers used across areas.", r".*"),
)

# Other source roots, rendered one section per directory. "src/" covers the
# small top-level packages shipped in the same wheel (harnest_postgres, ...).
PYTHON_ROOTS = ("src/", "studio/src/", "packages/", "official-extensions/", "scripts/")
GO_PATTERN = "*.go"


@dataclass(frozen=True, slots=True)
class Entry:
    """One source file and its one-line description (empty when missing)."""

    path: str
    description: str


@dataclass(slots=True)
class Section:
    """A heading in the map with its entries."""

    title: str
    blurb: str = ""
    entries: list[Entry] = field(default_factory=list)


def tracked_files(patterns: Sequence[str]) -> list[str]:
    """List tracked files, falling back to the filesystem outside a git checkout."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *patterns],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return sorted(line for line in result.stdout.splitlines() if (ROOT / line).is_file())
    except (OSError, subprocess.CalledProcessError):
        # git pathspec wildcards include nested paths; preserve that in archives.
        found = {
            path.relative_to(ROOT).as_posix()
            for pattern in patterns
            for path in (ROOT / Path(pattern).parent).rglob(Path(pattern).name)
            if path.is_file()
        }
        return sorted(found)


def first_paragraph(text: str) -> str:
    """Collapse the first paragraph of a docstring or comment to one line."""

    return " ".join(text.strip().split("\n\n")[0].split())


def python_description(path: Path) -> str:
    """Return the first paragraph of a module docstring."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return first_paragraph(ast.get_docstring(tree) or "")


def go_description(path: Path) -> str:
    """Return the first paragraph of the comment above a Go package clause."""

    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("package "):
            break
        if line.startswith("//go:") or line.startswith("// Code generated"):
            continue
        if line.startswith("//"):
            lines.append(line[2:].strip())
        elif lines and not line.strip():
            lines.append("")
    return first_paragraph("\n".join(lines))


def is_source(path: str) -> bool:
    """Exclude tests, fixtures, and test doubles from the map."""

    parts = path.split("/")
    name = parts[-1]
    if name.endswith("_test.go") or name.startswith("test_") or name == "conftest.py":
        return False
    return not any(part in {"tests", "_tests", "testdata", "node_modules"} for part in parts)


def area_for(module: str) -> str:
    """Pick the first area whose pattern matches a harnest module path."""

    for title, _, pattern in AREAS:
        if re.fullmatch(pattern, module):
            return title
    return AREAS[-1][0]


def package_sections(files: Iterable[str]) -> list[Section]:
    """Group src/harnest modules into areas, in AREAS order."""

    sections = {title: Section(title, blurb) for title, blurb, _ in AREAS}
    for path in files:
        module = path.removeprefix(PACKAGE).removesuffix(".py")
        sections[area_for(module)].entries.append(Entry(path, python_description(ROOT / path)))
    return [section for section in sections.values() if section.entries]


def directory_sections(files: Iterable[str], describe: str) -> list[Section]:
    """Group files by their directory, one section each."""

    sections: dict[str, Section] = {}
    for path in files:
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        section = sections.setdefault(directory, Section(f"{directory}/"))
        reader = go_description if describe == "go" else python_description
        section.entries.append(Entry(path, reader(ROOT / path)))
    return [sections[key] for key in sorted(sections)]


def collect() -> list[tuple[str, list[Section]]]:
    """Read every tracked source file into titled groups of sections."""

    python = [path for path in tracked_files(["*.py"]) if is_source(path)]
    harnest = [path for path in python if path.startswith(PACKAGE)]
    others = [path for path in python if path.startswith(PYTHON_ROOTS) and path not in harnest]
    go = [path for path in tracked_files([GO_PATTERN]) if is_source(path)]
    return [
        ("Python package `harnest` (src/harnest)", package_sections(harnest)),
        ("Other Python: bundled storage packages, Studio, companion packages, extensions, scripts",
         directory_sections(others, "python")),
        ("Go: the `harnest` CLI, engine, and internal packages", directory_sections(go, "go")),
    ]


def render(groups: Sequence[tuple[str, list[Section]]]) -> str:
    """Render the map as Markdown with links relative to docs/codebase."""

    out = [
        "# Every source file, in one line",
        "",
        "<!-- Generated by scripts/codebase_map.py; do not edit by hand. -->",
        "",
        "Generated from each file's own docstring (Python) or the comment above",
        "its `package` clause (Go). To change an entry, edit that docstring or",
        "comment, then run `python scripts/codebase_map.py`. To see how a file",
        "connects to others, run `python scripts/codebase_map.py deps <module>`.",
        "Start with [README.md](README.md) for how the pieces fit together.",
    ]
    for title, sections in groups:
        out += ["", f"## {title}"]
        for section in sections:
            out += ["", f"### {section.title}"]
            if section.blurb:
                out += ["", section.blurb]
            out.append("")
            for entry in section.entries:
                name = entry.path.removeprefix(PACKAGE) if entry.path.startswith(PACKAGE) else entry.path.rsplit("/", 1)[-1]
                out.append(f"- [`{name}`](../../{entry.path}): {entry.description or '**(undescribed)**'}")
    return "\n".join(out) + "\n"


def undescribed(groups: Sequence[tuple[str, list[Section]]]) -> list[str]:
    """List files that have no docstring or package comment."""

    return [entry.path for _, sections in groups for section in sections
            for entry in section.entries if not entry.description]


# -- Import graph ---------------------------------------------------------


def module_name(path: str) -> str:
    """Map src/harnest/a/b.py to harnest.a.b (packages drop __init__)."""

    dotted = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def imported_names(path: str, package: str) -> Iterator[str]:
    """Yield absolute module names imported anywhere in a file, lazy imports included."""

    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{anchor}.{base}" if base else anchor
            yield base
            yield from (f"{base}.{alias.name}" for alias in node.names)


def import_graph() -> dict[str, set[str]]:
    """Edges from each harnest module to the harnest modules it imports."""

    paths = [path for path in tracked_files(["src/harnest/*.py"]) if is_source(path)]
    modules = {module_name(path): path for path in paths}
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        package = name if path.endswith("__init__.py") else name.rsplit(".", 1)[0]
        targets = {target for target in imported_names(path, package) if target in modules}
        graph[name] = targets - {name}
    return graph


def resolve_target(graph: dict[str, set[str]], target: str) -> list[str]:
    """Match a module or package given as a path, dotted name, or short name."""

    wanted = "harnest." + target.removeprefix("src/harnest/").removeprefix("harnest.").removesuffix(".py").replace("/", ".")
    return sorted(name for name in graph if name == wanted or name.startswith(wanted + "."))


def print_deps(target: str) -> int:
    """Print what the target imports from Harnest and what imports it."""

    graph = import_graph()
    members = resolve_target(graph, target)
    if not members:
        print(f"no harnest module matches {target!r}", file=sys.stderr)
        return 1
    inside = set(members)
    imports = sorted({dep for name in members for dep in graph[name]} - inside)
    used_by = sorted({name for name, deps in graph.items() if deps & inside} - inside)
    print(f"{target}: {len(members)} module(s)")
    print(f"\nimports ({len(imports)}):")
    print("\n".join(f"  {name}" for name in imports) or "  (none)")
    print(f"\nimported by ({len(used_by)}):")
    print("\n".join(f"  {name}" for name in used_by) or "  (none)")
    return 0


def write_graph(destination: Path) -> int:
    """Write the whole Python import graph as JSON for other tools."""

    graph = import_graph()
    payload = {"modules": sorted(graph), "imports": {name: sorted(deps) for name, deps in sorted(graph.items())}}
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(graph)} modules to {destination}")
    return 0


# -- Command line ---------------------------------------------------------


def problems(*, write: bool = False) -> list[str]:
    """Return undescribed files and, unless rewriting, a stale-map notice."""

    groups = collect()
    rendered = render(groups)
    found = [f"{path}: add a module docstring or a comment above `package`" for path in undescribed(groups)]
    if write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8")
    elif not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
        found.append(f"{OUTPUT.relative_to(ROOT)} is stale; run python scripts/codebase_map.py")
    return found


def check_or_write(check: bool) -> int:
    """Rewrite the map, or report whether it is current and complete."""

    found = problems(write=not check)
    for message in found:
        print(message, file=sys.stderr)
    return 1 if found else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one of the map, deps, or graph commands."""

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if docs/codebase/files.md is stale")
    commands = parser.add_subparsers(dest="command")
    deps = commands.add_parser("deps", help="show what a module or package imports and what imports it")
    deps.add_argument("target", help="e.g. bundle, frameworks/adk, harnest.runtime")
    graph = commands.add_parser("graph", help="write the Python import graph")
    graph.add_argument("--json", type=Path, required=True, help="destination file")
    args = parser.parse_args(argv)
    if args.command == "deps":
        return print_deps(args.target)
    if args.command == "graph":
        return write_graph(args.json)
    return check_or_write(args.check)


if __name__ == "__main__":
    raise SystemExit(main())
