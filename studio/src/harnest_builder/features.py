"""Translate process-owned feature flags into Studio's HTTP boundary."""

from fastapi import HTTPException

from harnest._features import DEPLOYMENT_DISABLED, deployment_enabled


def require_deployment() -> None:
    """Keep both deployment routes and command jobs behind the same server opt-in."""
    if not deployment_enabled():
        raise HTTPException(403, DEPLOYMENT_DISABLED)
