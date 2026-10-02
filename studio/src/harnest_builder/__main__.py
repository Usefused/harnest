"""Launch the standalone builder against an explicitly selected local workspace."""

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import secrets
import shutil
import sys

import uvicorn

from .app import create_app


def main(*, arguments: Sequence[str] | None = None, embedded_packs: Sequence[Path] = (),
         cli_command: list[str] | tuple[str, ...] | None = None,
         trusted_ui: Sequence[str] = (), init_args: Sequence[str] = ()) -> None:
    """Launch standalone or company Studio with server-owned executable and pack bindings."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if arguments[:1] == ["pack"]:
        from .pack_cli import main as pack_main
        pack_main(arguments[1:])
        return
    parser = argparse.ArgumentParser(description="Harnest Agent Builder · built by Fused")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Existing agent folder or parent folder for projects")
    parser.add_argument("--port", type=int, default=1940)
    if cli_command is None:
        parser.add_argument("--cli", default=os.getenv("HARNEST_BUILDER_CLI", "harnest"), help="Harnest CLI executable")
    parser.add_argument("--pack", type=Path, action="append", default=[], help="Local Studio Pack folder (repeatable)")
    parser.add_argument("--safe-ui", action="store_true", help="Start with only the bundled default pack")
    parser.add_argument("--trust-ui-pack", action="append", default=[], help="Allow a named pack to execute JavaScript with full Studio access")
    options = parser.parse_args(arguments)
    command = tuple(cli_command) if cli_command is not None else (options.cli,)
    executable = shutil.which(command[0])
    if executable is None:
        parser.error(f"CLI executable not found: {command[0]}")
    if not options.workspace.is_dir():
        parser.error("--workspace must be an existing directory.")
    if not 1024 <= options.port <= 65535:
        parser.error("--port must be between 1024 and 65535.")
    from .packs import Packs
    from fastapi import HTTPException
    try:
        packs = Packs([] if options.safe_ui else [*embedded_packs, *options.pack])
    except (ValueError, OSError, HTTPException) as error:
        parser.error(str(getattr(error, "detail", error)))
    token = secrets.token_urlsafe(32)
    print(f"\nHarnest Agent Builder · Fused\nOpen http://127.0.0.1:{options.port}/#token={token}\nWorkspace: {options.workspace.resolve()}\n", flush=True)
    uvicorn.run(create_app(options.workspace, command, token=token, packs=packs,
                          trusted_ui=(*trusted_ui, *options.trust_ui_pack), safe_ui=options.safe_ui,
                          init_args=init_args), host="127.0.0.1", port=options.port, access_log=False)


if __name__ == "__main__":
    main()
