"""Read-only Fused discovery requests, authorized and fulfilled by the trusted Studio host."""
from harnest.agent import tool


@tool
def fused_discover(action: str, service_id: str = "", version: str = "", offset: int = 0) -> dict:
    """Request services, operations for a discovered service/version, or a page of servers.

    Finish this turn with the returned JSON; Studio adds sanitized discovery
    results to the next turn. Requires the user's Fused discovery permission.
    """
    return {"fused_discover": {"action": action, "service_id": service_id, "version": version, "offset": offset}}
