"""Validate and package company catalogs without starting Studio or executing pack code."""

import argparse
import json
from pathlib import Path

from fastapi import HTTPException

from .packs import Packs
from .pack_distribution import package_packs


def main(arguments=None):
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
    options = parser.parse_args(arguments)
    try:
        if options.action == "validate":
            print(json.dumps({"resources": Packs(options.packs).catalog()}, indent=2))
        else:
            print(package_packs(options.pack, options.output, options.name, options.version))
    except (ValueError, OSError, HTTPException) as error:
        parser.error(str(getattr(error, "detail", error)))
