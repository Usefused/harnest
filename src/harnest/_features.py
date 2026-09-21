"""Process-owned opt-ins for unreleased Harnest functionality."""

import os

DEPLOYMENT_FLAG = "HARNEST_ENABLE_DEPLOYMENT"
DEPLOYMENT_DISABLED = f"Deployment is disabled. Set {DEPLOYMENT_FLAG}=true to enable it."


def deployment_enabled() -> bool:
    """Require an explicit true value; missing, false, and invalid values stay off."""
    return os.environ.get(DEPLOYMENT_FLAG, "").strip().lower() == "true"


def require_deployment() -> None:
    """Reject deployment before filesystem, process, or infrastructure side effects."""
    from .provisioner_config import ProvisionError

    if not deployment_enabled():
        raise ProvisionError(DEPLOYMENT_DISABLED)
