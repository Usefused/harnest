"""A small embeddable company CLI using the same project planning API as other UIs."""
from __future__ import annotations

import argparse
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence, TextIO

from .contracts import ProjectError, ProjectPack
from .filesystem import apply_project_plan
from .planner import ProjectPlanner


def _option_converter(field: dict[str, Any]) -> Callable[[str], Any]:
    """Keep primitive flags ergonomic and accept explicit JSON for structured values."""
    converters = {'integer': int, 'number': float, 'string': str}
    if field.get('type') in converters:
        return converters[field['type']]
    return json.loads


def _resolved_field(schema: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    """Resolve local enum definitions emitted by Pydantic's JSON schema."""
    alternatives = [item for item in field.get('anyOf', []) if item.get('type') != 'null']
    if len(alternatives) == 1:
        return _resolved_field(schema, alternatives[0])
    reference = field.get('$ref', '')
    if reference.startswith('#/$defs/'):
        return schema['$defs'][reference.removeprefix('#/$defs/')]
    return field


def _pack_flags(parser: argparse.ArgumentParser, packs: Sequence[ProjectPack]) -> None:
    """Generate namespaced typed flags; validation remains in the shared planner."""
    for pack in packs:
        if pack.options is None:
            continue
        schema = pack.options.model_json_schema()
        for name, raw in schema.get('properties', {}).items():
            field = _resolved_field(schema, raw)
            flag = name.replace('_', '-')
            if len(packs) > 1:
                flag = f'{pack.name}-{flag}'
            parser.add_argument(f'--{flag}', dest=f'pack:{pack.name}:{name}', default=argparse.SUPPRESS,
                                type=_option_converter(field), choices=field.get('enum'),
                                help=raw.get('description', f'{pack.name} option: {name}'))


def _options(arguments: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Collect explicitly supplied inputs only, leaving defaults to the typed model."""
    result: dict[str, dict[str, Any]] = {}
    for key, value in vars(arguments).items():
        if key.startswith('pack:'):
            _, pack, name = key.split(':', 2)
            result.setdefault(pack, {})[name] = value.value if isinstance(value, Enum) else value
    return result


class ProjectCLI:
    """Embed init/upgrade planning and custom commands in a company-owned executable."""
    def __init__(self, name: str, packs: Sequence[ProjectPack], *, harnest_command: Sequence[str] = ('harnest',)) -> None:
        """Construct a parser without reading project files or running pack hooks."""
        self.planner = ProjectPlanner(packs, harnest_command=harnest_command)
        self.parser = argparse.ArgumentParser(prog=name)
        self._commands = self.parser.add_subparsers(dest='command', required=True)
        for command in ('init', 'upgrade'):
            parser = self._commands.add_parser(command)
            parser.add_argument('directory', type=Path)
            parser.add_argument('--json', action='store_true', help='print a content-free JSON plan')
            _pack_flags(parser, packs)
            self._command_flags(command, parser)

    def _command_flags(self, command: str, parser: argparse.ArgumentParser) -> None:
        """Expose scaffold and template choices without overriding template-owned settings."""
        if command == 'init':
            parser.add_argument('--dry-run', action='store_true')
            # None distinguishes an omitted framework from an explicit conflicting flag.
            parser.add_argument('--framework', choices=('adk', 'langgraph'), help='scaffold framework (default: adk)')
            parser.add_argument('--minimal', action='store_true')
            parser.add_argument('--template', help='Harnest template project, slug, or HTTPS wheel URL')
            parser.add_argument('--template-sha256', help='expected SHA-256 of an HTTPS template wheel')
        else:
            parser.add_argument('--apply', action='store_true')

    def add_command(self, name: str, handler: Callable[[argparse.Namespace], int], *, help: str = '') -> argparse.ArgumentParser:
        """Register a company command and return its parser for company-specific flags."""
        parser = self._commands.add_parser(name, help=help)
        parser.set_defaults(handler=handler)
        return parser

    def run(self, arguments: Sequence[str] | None = None, *, stdout: TextIO | None = None,
            stderr: TextIO | None = None) -> int:
        """Execute one command and return a process exit code; never exit the caller."""
        output, errors = stdout or sys.stdout, stderr or sys.stderr
        args = self.parser.parse_args(arguments)
        if hasattr(args, 'handler'):
            return args.handler(args)
        try:
            return self._run_project(args, output)
        except (ProjectError, OSError) as exc:
            print(f'{self.parser.prog}: {exc}', file=errors)
            return 1

    def _run_project(self, args: argparse.Namespace, output: TextIO) -> int:
        """Forward initialization choices and print the combined plan before applying it."""
        if args.command == 'init':
            plan = self.planner.plan_init(args.directory, options=_options(args),
                                          framework=args.framework, minimal=args.minimal,
                                          template=args.template, template_sha256=args.template_sha256)
            applying = not args.dry_run
        else:
            plan = self.planner.plan_upgrade(args.directory, options=_options(args))
            applying = args.apply
        print(json.dumps(plan.public(), indent=2) if args.json else plan.render(), file=output, flush=True)
        if plan.blockers:
            return 2
        if applying:
            backup = apply_project_plan(plan)
            if backup is not None and not args.json:
                print(f'Applied. Backup: {backup}', file=output)
        return 0
