"""Authenticated local folder browsing without reading arbitrary file contents."""

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Folder(BaseModel):
    """A user-selected absolute directory, never an executable or shell expression."""
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=4096)


def directory(value: str) -> Path:
    """Resolve an explicitly chosen directory while rejecting ambiguous relative paths."""
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise HTTPException(422, "Choose an existing absolute folder path.")
    return path.resolve()


def browse(value: str) -> dict:
    """List visible directories only; browsing alone never registers a project for editing."""
    root = directory(value)
    children = []
    for item in root.iterdir():
        if not item.name.startswith(".") and item.is_dir() and not item.is_symlink():
            children.append({"name": item.name, "path": str(item), "agent": (item / "config.yaml").is_file()})
        if len(children) > 1000:
            raise HTTPException(413, "Too many folders here. Enter a more specific folder path.")
    return {"path": str(root), "parent": str(root.parent), "agent": (root / "config.yaml").is_file(), "folders": sorted(children, key=lambda item: item["name"].lower())}


def install_routes(app, workspace) -> None:
    """Keep local selection behind the app's loopback and launch-token boundary."""
    @app.get("/api/folders")
    def folders(path: str = ""):
        """Browse from the launch folder unless the user supplies another location."""
        return browse(path or str(workspace.root))

    @app.post("/api/project/open")
    def open_folder(body: Folder):
        """Grant source access only to the exact agent folder explicitly selected by the user."""
        with workspace.lock:
            root = directory(body.path)
            if not (root / "config.yaml").is_file() or (root / "config.yaml").is_symlink():
                raise HTTPException(422, "Select an agent folder containing config.yaml.")
            return {"project": workspace.register(root)}
