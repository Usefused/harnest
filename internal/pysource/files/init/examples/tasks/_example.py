"""Copy to prepare_report.py to register application-owned queue work.

Direct calls run inline. Await prepare_report.defer(subject="daily") only from
an active Harnest runtime with the required task-storage configuration.
Tasks are not automatically exposed as model tools.
"""

from harnest.task import task


@task(queue="reports", max_retries=3)
async def prepare_report(subject: str) -> dict[str, str]:
    """Prepare a deterministic result without external side effects."""
    return {"subject": subject, "status": "ready"}
