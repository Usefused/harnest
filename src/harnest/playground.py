"""Bundled framework-neutral development playground routes."""

from __future__ import annotations

from importlib.resources import files
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


def create_playground_router(
    trace_store: PlaygroundTraceStore | None = None,
    eval_service: PlaygroundEvalService | None = None,
) -> Any:
    """Expose the same bundled UI for every Harnest runtime driver."""

    try:
        from fastapi import APIRouter, HTTPException
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

    return router


def _asset_path(filename: str) -> Any:
    """Resolve package data without depending on the caller's working directory."""

    return files("harnest").joinpath(_ASSET_DIRECTORY, filename)


def _headers(*, cache: bool) -> dict[str, str]:
    # The playground is a development surface and its stable asset paths span
    # package upgrades, so browsers must revalidate rather than retain stale UI.
    cache_control = "no-cache" if cache else "no-store"
    return {**_SECURITY_HEADERS, "Cache-Control": cache_control}


__all__ = ["create_playground_router"]
