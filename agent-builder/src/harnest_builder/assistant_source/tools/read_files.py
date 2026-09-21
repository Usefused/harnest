"""Request source through Studio's revision-checked, permission-aware boundary."""

from harnest.agent import tool


@tool
def read_files(paths: list[str]) -> dict:
    """Request missing project source; finish the turn with the returned JSON.

    Args:
        paths: Exact relative paths from context.project_files, at most 24.
            Request related files together. This tool does not read disk or run code.
    """
    return {"read_files": paths}
