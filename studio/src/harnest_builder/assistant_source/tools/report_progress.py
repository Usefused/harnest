"""Let the builder describe observable authoring work while its run is in flight."""

from harnest.agent import tool


@tool
def report_progress(message: str) -> dict:
    """Share a short user-facing plan or progress update, then continue working.

    Args:
        message: One sentence about the next action or completed work. Do not
            include private reasoning, credentials, source text or tool payloads.
    """
    return {"message": message[:280]}
