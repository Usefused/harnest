"""Propose an MCP plan without exposing management mutations or credentials to the model."""
import json
from harnest.agent import tool


@tool
def plan_fused_mcp(plan_json: str) -> dict:
    """Request a user-reviewed MCP plan using JSON fields from the Studio MCP planning contract.

    This does not provision anything. Never include project identity or bearer
    credentials. Finish the turn with this output so Studio can validate it.
    """
    return {"mcp_plan": json.loads(plan_json)}
