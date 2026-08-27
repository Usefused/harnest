"""Bundled framework-neutral development playground routes."""

from __future__ import annotations

from importlib.resources import files
from typing import Any


_ASSET_DIRECTORY = "_playground"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def create_playground_router() -> Any:
    """Expose the same bundled UI for every Harnest runtime driver."""

    try:
        from fastapi import APIRouter
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The development playground requires FastAPI") from exc

    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def playground() -> Any:
        return FileResponse(
            _asset_path("index.html"),
            media_type="text/html",
            headers=_headers(cache=False),
        )

    @router.get("/_harnest/playground.css", include_in_schema=False)
    async def playground_css() -> Any:
        return FileResponse(
            _asset_path("playground.css"),
            media_type="text/css",
            headers=_headers(cache=True),
        )

    @router.get("/_harnest/playground.js", include_in_schema=False)
    async def playground_javascript() -> Any:
        return FileResponse(
            _asset_path("playground.js"),
            media_type="text/javascript",
            headers=_headers(cache=True),
        )

    return router


def _asset_path(filename: str) -> Any:
    """Resolve package data without depending on the caller's working directory."""

    return files("harnest").joinpath(_ASSET_DIRECTORY, filename)


def _headers(*, cache: bool) -> dict[str, str]:
    # HTML revalidates so an upgraded runtime cannot retain references to older
    # assets; versioned wheels make short-lived asset caching safe.
    cache_control = "public, max-age=3600" if cache else "no-store"
    return {**_SECURITY_HEADERS, "Cache-Control": cache_control}


__all__ = ["create_playground_router"]
