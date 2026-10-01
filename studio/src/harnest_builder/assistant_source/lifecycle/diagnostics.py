"""Report provider failure categories without retaining prompts or exception messages."""

from contextlib import suppress
import json
import os
from pathlib import Path

from harnest import lifecycle


def _category(error) -> str:
    """Map provider exception metadata to a fixed, credential-free vocabulary."""
    name = type(error).__name__
    if name in {"Timeout", "TimeoutError", "ReadTimeout", "APITimeoutError"}:
        return "timeout"
    status = getattr(error, "status_code", None)
    categories = {401: "authentication", 403: "permission", 404: "model_missing", 429: "rate_limit",
                  500: "provider_unavailable", 502: "provider_unavailable", 503: "provider_unavailable", 504: "timeout"}
    if isinstance(status, int) and status in categories:
        return categories[status]
    return {"APIConnectionError": "connection", "ConnectionError": "connection",
            "ContextWindowExceededError": "context_limit", "BadRequestError": "provider_request"}.get(name, "model_error")


@lifecycle.model.on_error
def model_failure(context, error):
    """Pass only a session identity and safe category to the owning Studio supervisor."""
    destination = os.getenv("HARNEST_BUILDER_DIAGNOSTICS")
    if destination:
        with suppress(OSError):
            Path(destination).write_text(json.dumps({"session": context.session_id, "category": _category(error)}))


@lifecycle.model.after
def model_recovered(context, response):
    """Discard stale evidence when provider-internal recovery completes a model call."""
    destination = os.getenv("HARNEST_BUILDER_DIAGNOSTICS")
    if destination:
        with suppress(OSError):
            Path(destination).unlink(missing_ok=True)
