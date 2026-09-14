from harnest.agent import tool


@tool(description="search capability")
async def search(value: str) -> str:
    """Implement this tool's business logic in ordinary Python."""

    raise NotImplementedError("Implement this tool in ordinary Python")
