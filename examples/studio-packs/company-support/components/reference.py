from harnest.agent import tool

@tool
def reference(ticket_id: str) -> str:
    """Format an internal ticket reference without contacting a service."""
    return "SUP-" + ticket_id.strip().upper()
