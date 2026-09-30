"""Owner-scoped development administration over the serving task runtime."""

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .cron import CronConflictError, CronNotFoundError, CronRuntimeError, _registration_for, _validate_schedule_key
from .runtime_auth import principal_for
from .runtime_cron_store import StoredCronRuntime
from .runtime_task import TaskRuntimeDriver, TaskRuntimeError
from .runtime_task_store import ProviderTaskRuntimeManager
from .task_inspection import task_metadata


class CronCreate(BaseModel):
    """Allow only deployed targets and explicitly provided schedule arguments."""
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=256)
    expression: str = Field(min_length=1, max_length=128)
    arguments: dict = Field(default_factory=dict)


class CronEdit(BaseModel):
    """Require the revision reviewed by the user before changing a definition."""
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str = Field(min_length=1, max_length=128)
    arguments: dict | None = None
    revision: int = Field(ge=0)


class CronStatus(BaseModel):
    """Keep cancellation terminal and status actions explicit."""
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["active", "paused", "cancelled"]


def task_manager(driver):
    """Discover the serving manager through trusted runtime wrappers only."""
    seen = set()
    while driver is not None and id(driver) not in seen:
        seen.add(id(driver))
        if isinstance(driver, TaskRuntimeDriver) and isinstance(driver._manager, ProviderTaskRuntimeManager):
            return driver._manager
        driver = getattr(driver, "_driver", None)
    return None


def _owner(request, manager, scope):
    """Default to the verified principal; automation access requires a server-issued claim."""
    principal = principal_for(request)
    if scope == "automation":
        if principal.claims.get("harnest:work:automation") is not True:
            raise HTTPException(403, "Automation administration requires harnest:work:automation")
        return manager._automation_user_id
    return principal.user_id


def _same_origin(request):
    """Reject cross-origin browser mutations even for cookie or anonymous development auth."""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != str(request.base_url).rstrip("/")):
        raise HTTPException(403, "Use this runtime's same-origin Work view")


@asynccontextmanager
async def _operation(request, manager, scope):
    """Share readiness, ownership, and privacy-safe failures across administration routes."""
    if manager is None:
        raise HTTPException(503, "This agent has no durable task runtime. Configure task storage and serve a compiled task or cron function.")
    if request.method != "GET":
        _same_origin(request)
    owner = _owner(request, manager, scope)
    try:
        manager._require_ready()
        yield owner
    except CronNotFoundError:
        raise HTTPException(404, "Schedule not found") from None
    except CronConflictError:
        raise HTTPException(409, "Schedule changed or is terminal; refresh before retrying") from None
    except (ValueError, TypeError):
        raise HTTPException(422, "Invalid schedule, key, or arguments for the deployed cron function") from None
    except (CronRuntimeError, TaskRuntimeError):
        raise HTTPException(503, "Task or cron runtime is unavailable") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Work storage operation failed") from None


def _cron(manager, owner):
    """Reuse runtime validation and audit while binding authority to the authenticated caller."""
    return StoredCronRuntime(manager, manager.application.runtime_capabilities.cron_store,
                             enabled=manager._enable_cron, owner=owner, trigger="user")


def _schedule(record, *, include_arguments=False):
    """Project schedule metadata; arguments are read only for an explicit edit."""
    result = {name: getattr(record, name) for name in
              ("schedule_id", "key", "expression", "task_name", "status", "timezone", "next_run_at", "revision")}
    result["read_only"] = record.key.startswith("static:")
    if include_arguments:
        result["arguments"] = dict(record.arguments)
    return result


async def _editable(runtime, identity):
    """Keep application-owned declarations under source control across restarts."""
    record = await runtime._required(identity)
    if record.key.startswith("static:"):
        raise HTTPException(409, "Edit fixed schedules in cron/ and restart the agent")
    return record


def create_work_router(driver) -> APIRouter:
    """Mount development-only controls without adding administration to production APIs."""
    router = APIRouter(prefix="/_harnest/work", include_in_schema=False)
    manager = task_manager(driver)
    _install_task_routes(router, manager)
    _install_cron_routes(router, manager)

    @router.get("")
    async def capabilities(request: Request):
        """Describe available storage and authored targets without exposing arguments."""
        if manager is None:
            return {"available": False, "message": "Configure task storage and serve an agent with tasks or cron functions."}
        principal = principal_for(request)
        return {"available": True, "automation": principal.claims.get("harnest:work:automation") is True,
                "task_listing": callable(getattr(manager._store, "list_task_metadata", None)),
                "cron_listing": callable(getattr(manager.application.runtime_capabilities.cron_store, "list_cron_metadata", None)),
                "cron": manager._enable_cron and manager.application.runtime_capabilities.cron_store is not None,
                "targets": [item.authored.__name__ for item in manager.application.tasks if _registration_for(item.authored)],
                "fixed": [{"name": item.name, "expression": item.schedule, "task_name": item.task_name} for item in manager.application.crons]}

    return router


def _install_task_routes(router, manager):
    """Support optional metadata listing plus lookup/cancel for existing custom providers."""
    @router.get("/tasks")
    async def tasks(request: Request, scope: Literal["self", "automation"] = "self", after: str | None = Query(None, max_length=512), limit: int = Query(50, ge=1, le=100)):
        """Read one owner-scoped page using the provider's optional query capability."""
        async with _operation(request, manager, scope) as owner:
            listing = getattr(manager._store, "list_task_metadata", None)
            if not callable(listing):
                raise HTTPException(501, "This provider supports task lookup by ID but not listing")
            return await listing(application_id=manager.application.name, user_id=owner, after=after, limit=limit)

    @router.get("/tasks/{identity}")
    async def task(request: Request, identity: str, scope: Literal["self", "automation"] = "self"):
        """Keep inaccessible and missing task identities indistinguishable."""
        async with _operation(request, manager, scope) as owner:
            return task_metadata(await _task_record(manager, owner, identity))

    @router.post("/tasks/{identity}/cancel")
    async def cancel(request: Request, identity: str, scope: Literal["self", "automation"] = "self"):
        """Fence leases, audit the committed cancellation, and wake durable waiters."""
        async with _operation(request, manager, scope) as owner:
            record = await _task_record(manager, owner, identity)
            changed = await manager._cancel_record(replace(record, trigger="user"))
            if changed:
                await manager._publish_outcome(record.job_id, ("failed", "task_cancelled"))
            return {"cancelled": changed}


async def _task_record(manager, owner, identity):
    """Always pass application and verified owner predicates to the provider."""
    record = await manager._store.get_task(application_id=manager.application.name, user_id=owner, job_id=identity)
    if record is None:
        raise HTTPException(404, "Task not found")
    return record


def _install_cron_routes(router, manager):
    """Route cron mutations through the same validation, concurrency, and audit as tools."""
    @router.get("/crons")
    async def schedules(request: Request, scope: Literal["self", "automation"] = "self", after: str | None = Query(None, max_length=512), limit: int = Query(50, ge=1, le=100)):
        """Read a bounded owner page without returning stored task arguments."""
        async with _operation(request, manager, scope) as owner:
            runtime = _cron(manager, owner)
            runtime._owner()
            listing = getattr(runtime._store, "list_cron_metadata", None)
            if not callable(listing):
                raise HTTPException(501, "This provider supports schedule lookup by ID but not listing")
            return await listing(application_id=manager.application.name, user_id=owner, after=after, limit=limit)

    @router.post("/crons")
    async def create(request: Request, body: CronCreate, scope: Literal["self", "automation"] = "self"):
        """Create only a compiler-discovered target using a stable user-selected key."""
        async with _operation(request, manager, scope) as owner:
            _validate_schedule_key(body.key)
            if body.key.startswith("static:"):
                raise HTTPException(422, "The static: key prefix belongs to compiled schedules")
            result = await _cron(manager, owner).create_dynamic_schedule(**body.model_dump())
            return {"id": result.id}

    @router.get("/crons/{identity}")
    async def schedule(request: Request, identity: str, scope: Literal["self", "automation"] = "self"):
        """Fetch the definition explicitly selected for review by its owner."""
        async with _operation(request, manager, scope) as owner:
            return _schedule(await _cron(manager, owner)._required(identity), include_arguments=True)

    @router.patch("/crons/{identity}")
    async def edit(request: Request, identity: str, body: CronEdit, scope: Literal["self", "automation"] = "self"):
        """Reject stale edits and retain arguments when no replacement was supplied."""
        from .cron import _UNSET
        async with _operation(request, manager, scope) as owner:
            runtime = _cron(manager, owner)
            await _editable(runtime, identity)
            await runtime.update_dynamic_schedule(identity, expression=body.expression,
                arguments=_UNSET if body.arguments is None else body.arguments, expected_revision=body.revision)
            return {"updated": True}

    @router.post("/crons/{identity}/status")
    async def status(request: Request, identity: str, body: CronStatus, scope: Literal["self", "automation"] = "self"):
        """Pause, resume, or terminally cancel an owned dynamic schedule."""
        async with _operation(request, manager, scope) as owner:
            runtime = _cron(manager, owner)
            await _editable(runtime, identity)
            await runtime.set_dynamic_schedule_status(identity, body.status)
            return {"status": body.status}

    @router.delete("/crons/{identity}")
    async def delete(request: Request, identity: str, scope: Literal["self", "automation"] = "self"):
        """Delete the schedule only; already enqueued task occurrences remain independent."""
        async with _operation(request, manager, scope) as owner:
            runtime = _cron(manager, owner)
            await _editable(runtime, identity)
            return {"deleted": await runtime.delete_dynamic_schedule(identity)}
