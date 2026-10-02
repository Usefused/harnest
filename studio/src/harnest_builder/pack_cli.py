"""Validate and package company catalogs without starting Studio or executing pack code."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path

from fastapi import HTTPException

from .packs import Packs
from .pack_distribution import package_packs
from .ui_packs import UIPacks
from .ui_types import package_types


def main(arguments: Sequence[str] | None = None) -> None:
    """Expose one pack CLI for the standalone entrypoint and the Harnest command."""
    parser = argparse.ArgumentParser(prog="harnest-studio pack")
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate", help="Validate local pack manifests, compatibility, and sources")
    validate.add_argument("packs", type=Path, nargs="+")
    package = commands.add_parser("package", help="Build a company launcher wheel with embedded packs")
    package.add_argument("--pack", type=Path, action="append", required=True)
    package.add_argument("--name", required=True)
    package.add_argument("--version", required=True)
    package.add_argument("--output", type=Path, default=Path("dist"))
    types = commands.add_parser("types", help="Export the matching Studio UI TypeScript package")
    types.add_argument("--output", type=Path, default=Path("dist"))
    options = parser.parse_args(arguments)
    try:
        if options.action == "validate":
            catalog = Packs(options.packs)
            # Validation inspects all module declarations without executing or trusting their code.
            ui = UIPacks(catalog, trusted=catalog.packs).catalog()
            print(json.dumps({"resources": catalog.catalog(), "ui": ui}, indent=2))
        elif options.action == "types":
            print(package_types(options.output))
        else:
            print(package_packs(options.pack, options.output, options.name, options.version))
    except (ValueError, OSError, HTTPException) as error:
        parser.error(str(getattr(error, "detail", error)))
