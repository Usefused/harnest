"""Developer inspection of authored channel bindings, without starting an agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def add_channels_parser(subparsers: Any) -> None:
    """Share project selection and output contract across channel operations."""

    parser = subparsers.add_parser(
        "channels", help="inspect and fixture-test configured channel bindings"
    )
    parser.add_argument("operation", choices=("inspect", "test"))
    parser.add_argument("platform", nargs="?")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--json", action="store_true")


def run_channels_command(args: Any) -> int:
    """Discover local bindings and report readiness without any network I/O."""

    from ._library import authored_library
    from .channels import discover_channel_bindings, registered_channel_adapters

    _validate_request(args)
    project = args.project.resolve()
    with authored_library(project):
        directory = project / "channels"
        bindings = discover_channel_bindings(directory)
        if args.platform is not None:
            bindings = tuple(item for item in bindings if item.platform == args.platform)
            if not bindings:
                raise ValueError(f"no channel binding found for platform {args.platform!r}")
        if args.operation == "inspect":
            result = _inspect(bindings, registered_channel_adapters())
        else:
            result = _test(bindings, args.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


def _validate_request(args: Any) -> None:
    """Reject ambiguous invocations before importing any authored Python."""

    if args.operation == "test" and args.fixture is None:
        raise ValueError("channels test requires --fixture")
    if args.operation == "inspect" and args.fixture is not None:
        raise ValueError("--fixture is available only for channels test")


def _inspect(bindings: tuple[Any, ...], available_extensions: tuple[str, ...]) -> dict[str, Any]:
    """Report each binding's declared scope and whether its extension is loaded."""

    return {
        "available_extensions": list(available_extensions),
        "bindings": [
            {
                "platform": binding.platform,
                "extension": binding.extension,
                "extension_registered": binding.extension in available_extensions,
                "allowed_installations": list(binding.allowed_installations),
                "allowed_conversations": list(binding.allowed_conversations),
            }
            for binding in bindings
        ],
    }


def _test(bindings: tuple[Any, ...], fixture: Path) -> dict[str, Any]:
    """Normalize one offline fixture through each matching binding's adapter."""

    from dataclasses import asdict

    from .channels import create_channel_adapter

    raw = json.loads(fixture.read_text())
    results = []
    for binding in bindings:
        adapter = create_channel_adapter(binding.extension, binding.config)
        event = adapter.normalize_event(raw)
        results.append({
            "platform": binding.platform,
            "extension": binding.extension,
            "event": asdict(event),
        })
    return {"fixture": str(fixture), "results": results}
