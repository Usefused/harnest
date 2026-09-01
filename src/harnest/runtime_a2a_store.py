"""Official A2A task-store adapter for Harnest-owned persistence."""

from __future__ import annotations

from datetime import timezone

from google.protobuf.message import DecodeError

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token

from .checkpoint import (
    A2ATaskConflictError,
    A2ATaskCursorError,
    A2ATaskPersistence,
    A2ATaskRecord,
)


class HarnestA2ATaskStore(TaskStore):
    """Persist normative A2A protobufs within one Harnest application scope."""

    def __init__(self, persistence: A2ATaskPersistence, *, application_id: str) -> None:
        """Bind every task operation to one indexed Harnest persistence owner."""

        if not isinstance(persistence, A2ATaskPersistence):
            raise TypeError("persistence must implement A2ATaskPersistence")
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValueError("application_id must be non-empty text")
        self._persistence = persistence
        self._application_id = application_id

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Store a deterministic snapshot plus the minimum indexed projections."""

        timestamp = _task_status_timestamp(task)
        record = A2ATaskRecord(
            application_id=self._application_id,
            user_id=_owner(context),
            task_id=task.id,
            context_id=task.context_id,
            status=int(task.status.state),
            status_timestamp=timestamp,
            payload=task.SerializeToString(deterministic=True),
        )
        try:
            await self._persistence.put_a2a_task(record)
        except A2ATaskConflictError as exc:
            # A task ID may never be rebound to another conversation or owner.
            raise InvalidParamsError("A2A task ownership changed") from exc

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> Task | None:
        """Read one task only through its application and principal ownership key."""

        record = await self._persistence.get_a2a_task(
            application_id=self._application_id,
            user_id=_owner(context),
            task_id=task_id,
        )
        return None if record is None else _decode_task(record)

    async def list(
        self,
        params: ListTasksRequest,
        context: ServerCallContext,
    ) -> ListTasksResponse:
        """Delegate bounded filtering and inclusive cursor pagination to the store."""

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        cursor = _decode_cursor(params.page_token)
        try:
            page = await self._persistence.list_a2a_tasks(
                application_id=self._application_id,
                user_id=_owner(context),
                context_id=params.context_id or None,
                status=int(params.status) if params.status else None,
                status_timestamp_after=_request_timestamp(params),
                cursor_task_id=cursor,
                limit=page_size + 1,
            )
        except A2ATaskCursorError as exc:
            raise InvalidParamsError(
                f"Invalid page token: {params.page_token}"
            ) from exc
        visible = page.records[:page_size]
        next_token = (
            encode_page_token(page.records[page_size].task_id)
            if len(page.records) > page_size
            else ""
        )
        return ListTasksResponse(
            tasks=[_decode_task(record) for record in visible],
            next_page_token=next_token,
            total_size=page.total_size,
            page_size=page_size,
        )

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """Delete only the caller-owned snapshot without probing other owners."""

        await self._persistence.delete_a2a_task(
            application_id=self._application_id,
            user_id=_owner(context),
            task_id=task_id,
        )


def _owner(context: ServerCallContext) -> str:
    """Resolve the privacy-safe Harnest principal installed at the route boundary."""

    owner = context.user.user_name
    if not isinstance(owner, str) or not owner:
        raise RuntimeError("A2A request has no task owner")
    return owner


def _task_status_timestamp(task: Task) -> str | None:
    """Normalize the optional protobuf timestamp for backend-native comparisons."""

    if not task.HasField("status") or not task.status.HasField("timestamp"):
        return None
    return task.status.timestamp.ToDatetime(tzinfo=timezone.utc).isoformat()


def _request_timestamp(params: ListTasksRequest) -> str | None:
    """Normalize the optional lower-bound filter without inventing a timestamp."""

    if not params.HasField("status_timestamp_after"):
        return None
    return params.status_timestamp_after.ToDatetime(tzinfo=timezone.utc).isoformat()


def _decode_cursor(value: str) -> str | None:
    """Translate the normative opaque token into the store's inclusive task cursor."""

    if not value:
        return None
    try:
        cursor = decode_page_token(value)
    except (InvalidParamsError, TypeError, ValueError) as exc:
        raise InvalidParamsError(f"Invalid page token: {value}") from exc
    # Some permissive base64 decoders accept punctuation as an empty payload;
    # an empty task ID is never a valid datastore cursor.
    if not cursor:
        raise InvalidParamsError(f"Invalid page token: {value}")
    return cursor


def _decode_task(record: A2ATaskRecord) -> Task:
    """Fail closed if durable bytes no longer describe the indexed A2A task."""

    task = Task()
    try:
        task.ParseFromString(record.payload)
    except DecodeError as exc:
        raise RuntimeError("Stored A2A task is invalid") from exc
    if (
        task.id != record.task_id
        or task.context_id != record.context_id
        or int(task.status.state) != record.status
    ):
        raise RuntimeError("Stored A2A task index does not match its payload")
    return task


__all__ = ["HarnestA2ATaskStore"]
