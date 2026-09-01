"""CLI for exporting Python orchestrators into the Go runtime protocol."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .bundle import BundleError, compile_artifact
from .orchestrator import AgentSource, Orchestrator, define_orchestrator
from .testing import AgentTestError, run_agent_tests
from .upgrade import UpgradeError, apply_upgrade, plan_upgrade, render_upgrade_plan


def load_orchestrator(path: Path) -> Orchestrator:
    path = path.resolve()
    spec = importlib.util.spec_from_file_location("_harnest_user_orchestrator", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import orchestrator from {path}")
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(
        {
            "AgentSource": AgentSource,
            "Orchestrator": Orchestrator,
            "define_orchestrator": define_orchestrator,
        }
    )
    spec.loader.exec_module(module)
    value = getattr(module, "orchestrator", None)
    if not isinstance(value, Orchestrator):
        raise ValueError(f"{path} must export an Orchestrator named 'orchestrator'")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="export a Python orchestrator as JSON")
    plan_parser.add_argument("orchestrator", type=Path)
    compile_parser = subparsers.add_parser(
        "compile",
        help="compile and validate a filesystem agent",
    )
    compile_parser.add_argument("agent", type=Path)
    compile_parser.add_argument("--output", type=Path, required=True)
    compile_parser.add_argument(
        "--entrypoint",
        default="agent:root_agent",
        help="root export in agent:<symbol> form (default: agent:root_agent)",
    )
    compile_parser.add_argument(
        "--framework", choices=("adk", "langgraph"), default="adk"
    )
    compile_parser.add_argument(
        "--mode", choices=("managed", "advanced"), default="managed"
    )
    compile_parser.add_argument(
        "--enable-cli",
        action="store_true",
        help="allow the compiled artifact to invoke the agent from its launcher",
    )
    test_parser = subparsers.add_parser(
        "test",
        help="compile an agent and run its pytest suites",
    )
    test_parser.add_argument("agent", type=Path)
    test_parser.add_argument(
        "--smoke",
        action="store_true",
        help="run tests/smoke in addition to tests/unit",
    )
    test_parser.add_argument(
        "--framework", choices=("adk", "langgraph"), default="adk"
    )
    test_parser.add_argument(
        "--mode", choices=("managed", "advanced"), default="managed"
    )
    test_parser.add_argument(
        "--enable-cli",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    test_parser.add_argument(
        "--evals",
        action="store_true",
        help="run validated evals after Python tests pass",
    )
    test_parser.add_argument(
        "--eval-trajectory",
        choices=("business", "strict"),
        default="business",
        help="tool trajectory policy for evals (default: business)",
    )
    test_parser.add_argument(
        "--no-output",
        action="store_true",
        help="suppress output from unit, smoke, and eval tests",
    )
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="plan or apply an existing agent repository migration",
    )
    upgrade_parser.add_argument("agent", type=Path)
    upgrade_parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed destructive changes after creating backups",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            manifest = compile_artifact(
                args.agent,
                args.output,
                entrypoint=args.entrypoint,
                framework=args.framework,
                mode=args.mode,
                cli_enabled=args.enable_cli,
            )
            print(json.dumps(manifest, sort_keys=True))
            return 0
        if args.command == "test":
            return run_agent_tests(
                args.agent,
                include_smoke=args.smoke,
                include_evals=args.evals,
                eval_trajectory=args.eval_trajectory,
                no_output=args.no_output,
                framework=args.framework,
                mode=args.mode,
                cli_enabled=args.enable_cli,
            )
        if args.command == "upgrade":
            plan = plan_upgrade(args.agent)
            if not args.apply:
                print(render_upgrade_plan(plan), end="")
                return 2 if plan.blockers else 0
            # --apply is the consent boundary, but the fresh effective plan is
            # still flushed first so destructive work is never invisible.
            print(render_upgrade_plan(plan, applying=True), end="", flush=True)
            backup = apply_upgrade(plan)
            if backup is not None:
                print(f"Upgrade applied. Backup: {backup}")
            else:
                print("No changes applied.")
            return 0
        orchestrator = load_orchestrator(args.orchestrator)
        print(orchestrator.to_json(project_root=args.orchestrator.resolve().parent))
        return 0
    except (
        AgentTestError,
        BundleError,
        UpgradeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"harnest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
