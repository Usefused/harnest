"""Copy into an agent's lifecycle/ directory to opt into Threadify."""

from harnest import context, lifecycle
from harnest_threadify import BusinessFilter, Threadify

threadify = Threadify(
    service_name="support-agent",
    business_tools=("lookup_order", "issue_refund"),
    filter=BusinessFilter(attributes=("order.id", "ticket.category")),
)


@lifecycle.telemetry_exporter
def telemetry():
    """Export only session-linked business spans to Threadify."""
    return threadify.telemetry_exporter()


@lifecycle.resource
@context.provider("threadify")
def connection():
    """Own the async SDK connection and make native access explicit."""
    return threadify


@lifecycle.session.created
async def session_created(info, session):
    """Create or recover the thread after session persistence succeeds."""
    await threadify.session_created(info, session)


@lifecycle.agent.before
async def invocation(active, request):
    """Link existing sessions and all supported invocation transports."""
    return await threadify.before_invocation(active, request)


@lifecycle.http.scope
def request_scope(active, request):
    """Keep request correlation active until streaming finishes."""
    return threadify.request_scope(active, request)


@lifecycle.http.after
def response(active, head):
    """Record handled HTTP failures without response data."""
    return threadify.after_http(active, head)


@lifecycle.tool.after
def tool_completed(active, result):
    """Export selected business-tool outcomes without result payloads."""
    return threadify.tool_completed(active, result)


@lifecycle.tool.on_error
def tool_failed(active, error):
    """Export selected business-tool failures without exception text."""
    threadify.tool_failed(active, error)
