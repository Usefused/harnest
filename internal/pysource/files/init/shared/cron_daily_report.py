from harnest.cron import cron


# Schedules use UTC, independent of the host machine's local timezone.
@cron("0 9 * * 1-5", arguments={"subject": "daily"}, queue="reports", max_retries=3)
async def daily_report(subject: str) -> dict[str, str]:
    """Prepare a deterministic report without external side effects."""
    return {"subject": subject, "status": "ready"}
