"""Own one Threadify connection for both telemetry and contract checks."""

import os

from harnest import context, lifecycle
from harnest_threadify import Threadify


threadify = Threadify(
    service_name="refund-agent",
    connection_options={"engine_url": os.environ.get("THREADIFY_ENGINE_URL", "http://127.0.0.1:8081")},
)


@lifecycle.telemetry_exporter
def telemetry():
    """Keep session telemetry filtered by the integration's default policy."""
    return threadify.telemetry_exporter()


@lifecycle.resource
@context.provider("threadify")
def connection():
    """Expose the lifecycle-owned connection only inside an invocation."""
    return threadify


@lifecycle.session.created
async def session_created(info, session):
    """Link a committed Harnest session to its filtered telemetry thread."""
    await threadify.session_created(info, session)


@lifecycle.agent.before
async def invocation(active, request):
    """Recover older sessions and correlate business spans during the turn."""
    return await threadify.before_invocation(active, request)
