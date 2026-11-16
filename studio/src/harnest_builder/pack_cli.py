"""Studio-side pack tools: check studio sub-packs and export UI types, without starting Studio."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path

from fastapi import HTTPException

from .packs import Packs
from .ui_packs import UIPacks
from .ui_types import package_types


def main(arguments: Sequence[str] | None = None) -> None:
    """Back `harnest pack types` and a code-free check of studio sub-packs."""
    parser = argparse.ArgumentParser(prog="harnest-studio pack")
    commands = parser.add_subparsers(dest="action", required=True)
    validate = commands.add_parser("validate", help="Check the studio sub-pack of local packs")
    validate.add_argument("packs", type=Path, nargs="+")
    types = commands.add_parser("types", help="Export the matching Studio UI TypeScript package")
    types.add_argument("--output", type=Path, default=Path("dist"))
    options = parser.parse_args(arguments)
    try:
        if options.action == "validate":
            catalog = Packs(options.packs)
            # Validation inspects all module declarations without executing or trusting their code.
            ui = UIPacks(catalog, trusted=catalog.packs).catalog()
            print(json.dumps({"resources": catalog.catalog(), "ui": ui}, indent=2))
        else:
            print(package_types(options.output))
    except (ValueError, OSError, HTTPException) as error:
        parser.error(str(getattr(error, "detail", error)))
