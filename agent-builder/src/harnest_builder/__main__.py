"""Launch the standalone builder against an explicitly selected local workspace."""

import argparse
import os
from pathlib import Path
import secrets
import shutil

import uvicorn

from .app import create_app


def main() -> None:
    """Print a private launch URL and bind only to loopback with the selected Harnest CLI."""
    parser = argparse.ArgumentParser(description="Harnest Agent Builder · built by Fused")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Existing agent folder or parent folder for projects")
    parser.add_argument("--port", type=int, default=1940)
    parser.add_argument("--cli", default=os.getenv("HARNEST_BUILDER_CLI", "harnest"), help="Harnest CLI executable")
    options = parser.parse_args()
    executable = shutil.which(options.cli)
    if executable is None:
        parser.error("Harnest CLI not found. Install Harnest or pass --cli /path/to/harnest.")
    if not options.workspace.is_dir():
        parser.error("--workspace must be an existing directory.")
    if not 1024 <= options.port <= 65535:
        parser.error("--port must be between 1024 and 65535.")
    token = secrets.token_urlsafe(32)
    print(f"\nHarnest Agent Builder · Fused\nOpen http://127.0.0.1:{options.port}/#token={token}\nWorkspace: {options.workspace.resolve()}\n", flush=True)
    uvicorn.run(create_app(options.workspace, executable, token=token), host="127.0.0.1", port=options.port, access_log=False)


if __name__ == "__main__":
    main()
