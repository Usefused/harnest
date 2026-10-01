"""Offline deployment review and credential-presence checks for Studio's guided flow."""

import os
import re

from fastapi import HTTPException

from harnest.provisioner import Provisioner
from harnest.provisioner_config import MANIFEST, ProvisionError, parse_manifest
from harnest.provisioner_plan import Plan
from .files import read


def overview(root, environment: str) -> dict:
    """Separate configured intent, last recorded outcome, and live status checked through CLI jobs."""
    try:
        service = Provisioner(root, environment)
        document = read(root, MANIFEST)
        plan = Plan(parse_manifest(document["text"], environment), environment, str(root))
        required = _required(plan)
        missing = [key for key in required if not os.environ.get(key)]
        blockers = []
        if not plan.deployment.agents:
            blockers.append("No agents configured. Add an agent image to the deployment configuration.")
        if missing:
            blockers.append("Set these variables in the environment used to start Studio, then restart Studio: " + ", ".join(missing))
        return {"plan": plan.summary(), "manifest_revision": document["revision"], "required_variables": required,
                "missing_variables": missing, "blockers": blockers, "recorded": service.recorded(),
                "history": service.history()}
    except ProvisionError as error:
        raise HTTPException(422, str(error)) from None


def _required(plan) -> list[str]:
    """Report only secret-reference names; never return or evaluate their runtime values."""
    names = set()
    for name in plan.workloads:
        for value in plan.environment_for(name, None).values():
            match = re.fullmatch(r"<secret:([A-Za-z_][A-Za-z0-9_]*)>", value)
            if match:
                names.add(match[1])
    return sorted(names)


def install_routes(app, workspace) -> None:
    """Keep dashboard reads bounded and separate from explicit infrastructure commands."""
    @app.get("/api/deployment/overview")
    def inspect(project: str, environment: str = "local"):
        """Validate and summarize the on-disk deployment without applying resources."""
        with workspace.lock:
            return overview(workspace.project(project), environment)

    @app.get("/api/deployment/history")
    def history(project: str, environment: str = "local", before: int | None = None):
        """Page older revision metadata without loading snapshots or resolved credentials."""
        try:
            with workspace.lock:
                return Provisioner(workspace.project(project), environment).history(before=before)
        except ProvisionError as error:
            raise HTTPException(422, str(error)) from None
