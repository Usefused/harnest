"""Python side of `harnest pack ...` and of `harnest init|upgrade --pack`.

The Go CLI parses user-facing flags and calls ``python -m harnest.cli pack
ACTION``. Planning runs core ``harnest init``/upgrade in a staging copy first,
then the packs' hooks, and prints one combined plan before anything is written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence, TextIO

from ..cli import cli_name
from ..frameworks.registry import framework_names
from .contracts import ProjectError, ProjectPlan
from .filesystem import apply_project_plan
from .packs import check_studio, load_pack, load_packs, parse_options
from .planner import ProjectPlanner


def run_pack_command(arguments: Sequence[str], *, stdout: TextIO | None = None,
                     stderr: TextIO | None = None) -> int:
    """Dispatch one pack action and return its exit code (2 for blocked plans)."""
    output, errors = stdout or sys.stdout, stderr or sys.stderr
    arguments = list(arguments)
    if arguments[:1] == ["run"]:
        return _run(arguments[1:], output, errors)
    args = _parser().parse_args(arguments)
    try:
        if args.action == "init":
            return _init(args, output)
        if args.action == "upgrade":
            return _upgrade(args, output)
        if args.action == "validate":
            return _validate(args.packs, output)
        if args.action == "build":
            from .distribution import build_pack
            print(f"Built {build_pack(args.pack, args.output)}", file=output)
            return 0
        if args.action == "prepare-binary":
            from .binary_payload import prepare_pack_binary
            manifest = prepare_pack_binary(args.pack, args.staging, uv_executable=args.uv)
            print(json.dumps(manifest, sort_keys=True), file=output)
            return 0
    except (ProjectError, OSError) as exc:
        print(f"{cli_name()}: {exc}", file=errors)
        return 1
    raise AssertionError(args.action)


def _parser() -> argparse.ArgumentParser:
    """Build the internal pack subcommand parser using the active branded CLI name."""
    parser = argparse.ArgumentParser(prog=f"{cli_name()} pack")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init")
    init.add_argument("directory", type=Path)
    _plan_flags(init)
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--framework", choices=framework_names())
    init.add_argument("--mode", choices=("managed", "advanced"))
    init.add_argument("--minimal", action="store_true")
    init.add_argument("--example", action="store_true")
    init.add_argument("--template")
    init.add_argument("--template-sha256")
    upgrade = actions.add_parser("upgrade")
    upgrade.add_argument("directory", type=Path)
    _plan_flags(upgrade)
    upgrade.add_argument("--apply", action="store_true")
    validate = actions.add_parser("validate")
    validate.add_argument("packs", type=Path, nargs="+")
    build = actions.add_parser("build")
    build.add_argument("pack", type=Path)
    build.add_argument("--output", type=Path, default=Path("dist"))
    prepare = actions.add_parser("prepare-binary")
    prepare.add_argument("pack", type=Path)
    prepare.add_argument("--staging", type=Path, required=True)
    prepare.add_argument("--uv", default=None)
    return parser


def _plan_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pack", dest="packs", type=Path, action="append", required=True)
    parser.add_argument("--option", dest="options", action="append", default=[])
    parser.add_argument("--harnest", required=True, help="Harnest executable used for core init")
    parser.add_argument("--json", action="store_true", help="print a content-free JSON plan")


def _planner(args: argparse.Namespace) -> tuple[ProjectPlanner, dict]:
    packs = load_packs(args.packs)
    missing = [pack.name for pack in packs if pack.project is None]
    if missing:
        raise ProjectError(f"{', '.join(missing)}: no pack.py, so there is nothing to apply to agents")
    options = parse_options(args.options, packs)
    return ProjectPlanner([pack.project for pack in packs], harnest_command=(args.harnest,)), options


def _init(args: argparse.Namespace, output: TextIO) -> int:
    """Plan and optionally apply a new agent scaffold using the selected packs."""
    planner, options = _planner(args)
    plan = planner.plan_init(args.directory, options=options, framework=args.framework,
                             minimal=args.minimal, mode=args.mode, example=args.example,
                             template=args.template, template_sha256=args.template_sha256)
    return _finish(plan, applying=not args.dry_run, as_json=args.json, output=output, is_init=True)


def _upgrade(args: argparse.Namespace, output: TextIO) -> int:
    """Plan and optionally apply core and pack migrations to an existing agent."""
    planner, options = _planner(args)
    plan = planner.plan_upgrade(args.directory, options=options)
    code = _finish(plan, applying=args.apply, as_json=args.json, output=output, is_init=False)
    # Remind the user how to commit a clean upgrade preview when changes are pending.
    if code == 0 and not args.apply and not args.json and plan.changes:
        print("Run the same command with --apply to make these changes.", file=output)
    return code


def _finish(
    plan: ProjectPlan, *, applying: bool, as_json: bool, output: TextIO, is_init: bool = False,
) -> int:
    """Render one project plan, apply it when authorized, and report the completion summary."""
    print(json.dumps(plan.public(), indent=2) if as_json else plan.render(), file=output, end="" if not as_json else "\n", flush=True)
    # Block application whenever planning produced unresolved conflicts or validation errors.
    if plan.blockers:
        return 2
    # Apply the reviewed plan only when the caller authorized filesystem writes.
    if applying:
        backup = apply_project_plan(plan)
        if backup is not None and not as_json:
            # Fresh init projects have no prior files backed up, so avoid printing a misleading backup path.
            if is_init:
                print(f"Initialized {plan.root}", file=output)
            else:
                print(f"Applied. Backup: {backup}", file=output)
    return 0


def _validate(roots: Sequence[Path], output: TextIO) -> int:
    """Validate manifests, hooks, Studio sub-packs, and binary payload declarations."""
    from .binary_payload import validate_pack_payload

    packs = load_packs(roots)
    for pack in packs:
        studio = check_studio(pack)
        validate_pack_payload(pack)
        project = pack.project
        print(f"{pack.name} {pack.manifest.version}: command `{pack.manifest.command_name}`", file=output)
        print(f"  project hooks: {'schema ' + str(project.schema_version) if project else 'none'}", file=output)
        commands = ", ".join(item.name for item in project.commands) if project else ""
        print(f"  commands: {commands or 'none'}", file=output)
        print(f"  studio: {studio}", file=output)
        print(f"  skills: {len(pack.manifest.skills)}", file=output)
        print(f"  packages: {len(pack.manifest.packages)}", file=output)
        print(f"  extensions: {len(pack.manifest.extensions)}", file=output)
    return 0


def _run(arguments: list[str], output: TextIO, errors: TextIO) -> int:
    """Run one company command: `harnest pack run PACK COMMAND [ARGS...]`."""
    prog = cli_name()
    # Require both the pack folder and command name before loading pack hooks.
    if len(arguments) < 2:
        print(f"usage: {prog} pack run PACK COMMAND [ARGS...]", file=errors)
        return 2
    root, name, rest = Path(arguments[0]), arguments[1], arguments[2:]
    try:
        pack = load_pack(root)
    except (ProjectError, OSError) as exc:
        print(f"{prog}: {exc}", file=errors)
        return 1
    commands = {item.name: item for item in (pack.project.commands if pack.project else ())}
    # Report available commands with the active CLI prefix when an unknown command is requested.
    if name not in commands:
        known = ", ".join(sorted(commands)) or "none"
        print(f"{prog}: pack {pack.name} has no command {name!r} (commands: {known})", file=errors)
        return 2
    result = commands[name].handler(list(rest))
    return int(result or 0)


__all__ = ["run_pack_command"]
