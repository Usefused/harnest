"""Private, payload-free projection for optional task administration queries."""

from __future__ import annotations


from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .task_storage import TaskRecord

CRON_FIELDS = ("schedule_id", "key", "expression", "task_name", "status", "timezone", "next_run_at", "revision")

TASK_FIELDS = ("job_id", "task_name", "queue", "status", "scheduled_at", "attempt",
               "max_retries", "created_at", "updated_at", "failure_code")


def task_metadata(record: TaskRecord) -> dict[str, Any]:
    """Exclude invocation, arguments, results, permissions, and lease credentials."""
    return {name: getattr(record, name) for name in TASK_FIELDS}


def validate_page(limit: int) -> None:
    """Keep optional provider inspection bounded even outside HTTP routes."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def task_page(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Use opaque job IDs as exclusive cursors, including a possibly empty final page."""
    return {"items": items, "after": items[-1]["job_id"] if len(items) == limit else None, "indexing": False}


def cron_page(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """Identify code-owned schedules without disclosing their arguments."""
    for item in items:
        item["read_only"] = item["key"].startswith("static:")
    return {"items": items, "after": items[-1]["schedule_id"] if len(items) == limit else None}
