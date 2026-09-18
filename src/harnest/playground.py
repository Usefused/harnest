"""Framework-neutral development routes backed by CLI-owned browser assets."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

try:
    from starlette.requests import Request
except ImportError:  # pragma: no cover - optional runtime dependency
    Request = Any  # type: ignore[misc,assignment]

if TYPE_CHECKING:
    from .playground_eval import PlaygroundEvalService
    from .playground_trace import PlaygroundTraceStore


_ASSET_DIRECTORY = "_playground"
_asset_override: ContextVar[Path | None] = ContextVar("playground_assets", default=None)
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class _EvalRunRequest(BaseModel):
    """Strict body for one local playground evaluation run."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    suite_id: str = Field(alias="suiteId", min_length=1)
    trajectory: str = "business"


@contextmanager
def playground_assets(directory: Path | None):
    """Scope CLI-owned assets to app construction, not global process state."""

    token = _asset_override.set(directory)
    try:
        yield
    finally:
        _asset_override.reset(token)


def playground_available() -> bool:
    """Installed production wheels have no UI; source checkouts support development."""

    return _asset_path("index.html").is_file()


def create_playground_router(
    trace_store: PlaygroundTraceStore | None = None,
    eval_service: PlaygroundEvalService | None = None,
    *, openapi_enabled: bool = True,
    mcp_service: Any | None = None,
    studio_service: Any | None = None,
    connectors_service: Any | None = None,
) -> Any:
    """Capture development asset ownership before handling asynchronous requests."""

    try:
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The development playground requires FastAPI") from exc

    router = APIRouter()
    directory = _asset_path("index.html").parent

    @router.get("/", include_in_schema=False)
    async def playground() -> Any:
        """Omit disabled API links even before JavaScript loads."""

        contents = (directory / "index.html").read_text(encoding="utf-8")
        if not openapi_enabled:
            before, _, remainder = contents.partition("<!-- openapi:start -->")
            _, _, after = remainder.partition("<!-- openapi:end -->")
            contents = before + after
        return HTMLResponse(
            contents,
            headers=_headers(cache=False),
        )

    @router.get("/_harnest/playground.css", include_in_schema=False)
    async def playground_css() -> Any:
        return FileResponse(
            directory / "playground.css",
            media_type="text/css",
            headers=_headers(cache=True),
        )

    @router.get("/_harnest/playground.js", include_in_schema=False)
    async def playground_javascript() -> Any:
        return FileResponse(
            directory / "playground.js",
            media_type="text/javascript",
            headers=_headers(cache=True),
        )

    @router.get("/_harnest/selects.css", include_in_schema=False)
    @router.get("/_harnest/selects.js", include_in_schema=False)
    @router.get("/_harnest/builder.js", include_in_schema=False)
    @router.get("/_harnest/builder.css", include_in_schema=False)
    @router.get("/_harnest/studio.js", include_in_schema=False)
    @router.get("/_harnest/studio.css", include_in_schema=False)
    @router.get("/_harnest/markdown.js", include_in_schema=False)
    @router.get("/_harnest/markdown-it.min.js", include_in_schema=False)
    async def playground_markdown(request: Request) -> Any:
        """Serve fixed auxiliary assets, never arbitrary directory contents."""

        filename = request.url.path.rsplit("/", 1)[-1]
        return FileResponse(
            directory / filename,
            media_type="text/css" if filename.endswith(".css") else "text/javascript",
            headers=_headers(cache=True),
        )

    from .playground_studio import install_studio_routes

    install_studio_routes(router, studio_service)
    if studio_service is not None:
        from .playground_builder import install_builder_routes
        install_builder_routes(router, studio_service)

    if trace_store is not None:
        from .runtime_auth import principal_for

        @router.get("/_harnest/traces", include_in_schema=False)
        async def playground_traces(
            request: Request, sessionId: str | None = None
        ) -> dict[str, Any]:
            traces = trace_store.list(
                user_id=principal_for(request).user_id,
                session_id=sessionId,
            )
            return {"traces": traces}

        @router.get("/_harnest/traces/{trace_id}", include_in_schema=False)
        async def playground_trace(trace_id: str, request: Request) -> dict[str, Any]:
            trace = trace_store.get(
                trace_id,
                user_id=principal_for(request).user_id,
            )
            if trace is None:
                raise HTTPException(status_code=404, detail="Trace not found")
            return trace

    if eval_service is not None:

        @router.get("/_harnest/evals", include_in_schema=False)
        async def playground_evals() -> dict[str, Any]:
            return eval_service.catalog()

        @router.post("/_harnest/evals/run", include_in_schema=False)
        async def playground_eval_run(request: _EvalRunRequest) -> dict[str, Any]:
            """Separate runtime failures from invalid requests and scored outcomes."""
            from .evaluation import EvaluationError
            from .eval_errors import EvaluationExecutionError

            try:
                return await eval_service.run(request.suite_id, request.trajectory)
            except KeyError:
                raise HTTPException(status_code=404, detail="Eval suite not found")
            except EvaluationExecutionError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            except (EvaluationError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    if mcp_service is not None:
        from .playground_mcp import install_mcp_routes

        install_mcp_routes(router, mcp_service)

    from .playground_connectors import install_connector_routes

    install_connector_routes(router, connectors_service)
    return router


def _asset_path(filename: str) -> Any:
    """Resolve CLI assets or editable source files, absent from production wheels."""

    directory = _asset_override.get()
    if directory is not None:
        return directory / filename
    return files("harnest").joinpath(_ASSET_DIRECTORY, filename)


def _headers(*, cache: bool) -> dict[str, str]:
    # The playground is a development surface and its stable asset paths span
    # package upgrades, so browsers must revalidate rather than retain stale UI.
    cache_control = "no-cache" if cache else "no-store"
    return {**_SECURITY_HEADERS, "Cache-Control": cache_control}


__all__ = ["create_playground_router"]
