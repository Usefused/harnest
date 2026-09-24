"""Compile and serve the example through the production Harnest runtime."""
import argparse
from pathlib import Path

import uvicorn
from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app


def main():
    """Keep each backend in its own process and compiled artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", choices=("adk", "langgraph"), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    # Keep generated runtime files outside the authored example tree.
    artifact = root.parents[1] / ".harnest" / "copilotkit" / args.framework
    compile_artifact(root / "agent", artifact, framework=args.framework)
    app = create_fastapi_app(artifact, playground_enabled=False, live_enabled=False)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
