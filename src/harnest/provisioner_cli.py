"""Public provisioner command used by the native CLI and Studio job supervisor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .provisioner import Provisioner
from .provisioner_config import ProvisionError


def main(argv: list[str] | None = None) -> int:
    """Run a reviewed manifest without compiling or importing authored agent code."""

    parser = argparse.ArgumentParser(description="Provision agent images and services, or connect existing services")
    parser.add_argument("operation", choices=("init", "plan", "apply", "status", "stop", "remove", "history", "rollback"))
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--environment", default="local")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--before-revision", type=int)
    args = parser.parse_args(argv)
    try:
        service = Provisioner(args.project, args.environment)
        result = dispatch(service, args)
    except (ProvisionError, OSError):
        # Validation and process boundaries already remove secret-bearing details.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, ProvisionError) else "Provisioner filesystem operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


STARTER = """# Add independently deployable agent images under agents.
version: 1
name: app
backend: local
services:
  cache:
    mode: provision
    type: redis
    image: redis:7.4
    ports:
      redis: 6379
    command: [redis-server, --appendonly, 'yes']
    healthcheck:
      command: [redis-cli, ping]
    persistence:
      mount: /data
      size: 5Gi
    provides:
      REDIS_URL: redis://${services.cache.host}:${services.cache.ports.redis}/0
agents: {}
# An environment can replace cache with an externally owned service:
# environments:
#   production:
#     backend: kubernetes
#     context: your-k3s-context
#     namespace: your-existing-namespace
#     services:
#       cache:
#         mode: connect
#         type: redis
#         url: {secret: REDIS_URL}
#         variable: REDIS_URL
"""


def initialize(root: Path) -> dict:
    """Create an editable starter without overwriting an existing declaration or starting services."""

    from .provisioner_config import MANIFEST
    try:
        with (root / MANIFEST).open("x") as stream:
            stream.write(STARTER)
    except FileExistsError:
        raise ProvisionError("harnest-deployment.yaml already exists; edit it instead") from None
    return {"created": MANIFEST}


def dispatch(service: Provisioner, args) -> dict:
    """Validate revision selection before dispatch so unused options cannot imply a rollback."""

    if args.operation == "rollback" and args.revision is None:
        raise ProvisionError("Rollback requires --revision; use history to choose a successful revision")
    if args.revision is not None and args.operation not in {"plan", "rollback"}:
        raise ProvisionError("--revision is supported only by plan and rollback")
    if args.operation != "history" and (args.before_revision is not None or args.limit != 20):
        raise ProvisionError("History pagination options require the history operation")
    return _selected_operation(service, args)


def _selected_operation(service: Provisioner, args) -> dict:
    """Share revision-aware operations across the native CLI and Builder jobs."""

    if args.operation == "init":
        return initialize(args.project)
    if args.operation == "history":
        return service.history(args.limit, args.before_revision)
    if args.operation == "rollback":
        return service.rollback(args.revision)
    if args.operation == "plan":
        return service.plan(args.revision)
    return _execute(service, args.operation)


def _execute(service: Provisioner, operation: str) -> dict:
    """Keep state-changing actions explicit and never interpret browser-supplied commands."""

    if operation in {"stop", "remove"}:
        return service.control(operation)
    return getattr(service, operation)()


if __name__ == "__main__":
    raise SystemExit(main())
