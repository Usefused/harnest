"""Read-only architecture for the single compiled agent owned by a playground."""

from __future__ import annotations

from functools import cached_property
import hashlib
from pathlib import Path
from typing import Any

from starlette.requests import Request

from .playground_inspector import StudioInspectionError, inspect_workspace

_SOURCE_LIMIT = 1024 * 1024
_SOURCE_SUFFIXES = frozenset({".py", ".md", ".mdx", ".yaml", ".yml", ".json", ".toml"})


class PlaygroundStudioService:
    """Bind inspection to compiled source; never accept a browser-selected workspace."""

    def __init__(self, source: Path, *, mode: str | None = None, framework: str = "adk") -> None:
        """Capture the artifact path without inspecting or importing authored modules."""

        self.source = source.resolve()
        self.mode = mode
        self.framework = framework

    @cached_property
    def projection(self) -> dict[str, Any]:
        """Keep one deterministic snapshot per runtime, independent of authoring edits."""

        value = inspect_workspace(self.source, require_config=False, mode=self.mode).to_dict()
        # Deployment paths are implementation details, not part of the browser contract.
        value.pop("workspace_path")
        value["read_only"] = True
        for file in value["files"]:
            if Path(file["path"]).name == "instructions.md":
                value["blocks"].append({
                    "id": f"{file['path']}:instructions", "kind": "instructions",
                    "name": "Instructions", "path": file["path"],
                    "line": 1, "end_line": 1, "config": {},
                })
        return value

    def source_file(self, path: str) -> dict[str, str]:
        """Read only indexed text matching the snapshot; reject traversal and links."""

        from fastapi import HTTPException

        record = next((item for item in self.projection["files"] if item["path"] == path), None)
        if record is None or Path(path).suffix not in _SOURCE_SUFFIXES:
            raise HTTPException(status_code=404, detail="Source file not available")
        candidate = self.source / path
        if not _contained_file(self.source, candidate):
            raise HTTPException(status_code=404, detail="Source file not available")
        with candidate.open("rb") as stream:
            contents = stream.read(_SOURCE_LIMIT + 1)
        if len(contents) > _SOURCE_LIMIT:
            raise HTTPException(status_code=413, detail="Source preview exceeds 1 MiB")
        if hashlib.sha256(contents).hexdigest() != record["digest"]:
            raise HTTPException(status_code=409, detail="Build source changed; restart the server to inspect the new build")
        return {"path": path, "text": contents.decode("utf-8")}


def _contained_file(root: Path, path: Path) -> bool:
    """Reject linked components as well as resolved escapes from the artifact."""

    if not path.resolve().is_relative_to(root):
        return False
    relative = path.relative_to(root)
    return not any((root.joinpath(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)) and path.is_file()


def _local_source_access(request: Request) -> bool:
    """Raw authored text is local-only, including host and browser-origin checks."""

    local = {"localhost", "127.0.0.1", "::1"}
    if request.client is None or request.client.host not in local:
        return False
    if request.url.hostname not in local:
        return False
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin", expected) != expected:
        return False
    return request.headers.get("sec-fetch-site") != "cross-site"


def install_studio_routes(router: Any, service: PlaygroundStudioService | None) -> None:
    """Install authenticated metadata reads and local-only source previews, never edits."""

    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    @router.get("/_harnest/studio", include_in_schema=False)
    def architecture(request: Request) -> Any:
        """Inspect in the thread pool and omit local paths and raw source from metadata."""

        if service is None:
            raise HTTPException(status_code=404, detail="Architecture is unavailable for this runtime; serve a compiled Harnest agent")
        try:
            value = {**service.projection, "source_available": _local_source_access(request)}
        except (OSError, ValueError, RecursionError, StudioInspectionError):
            raise HTTPException(status_code=422, detail="Unable to inspect this build's source") from None
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @router.get("/_harnest/studio/source", include_in_schema=False)
    def source(request: Request, path: str) -> Any:
        """Apply access checks before touching any authored file contents."""

        if not _local_source_access(request):
            raise HTTPException(status_code=403, detail="Source previews are available only in local development")
        if service is None:
            raise HTTPException(status_code=404, detail="Source is unavailable for this runtime")
        try:
            value = service.source_file(path)
        except (OSError, ValueError, RecursionError, StudioInspectionError):
            raise HTTPException(status_code=422, detail="Unable to read this build's source") from None
        return JSONResponse(value, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
