"""Privacy-safe provider-boundary evidence for the gated live journey."""

from __future__ import annotations

import json
import os
from pathlib import Path

from harnest.lifecycle import lifecycle


def _record(phase: str) -> None:
    """Record only boundary direction so provider content never enters evidence."""

    target = os.environ.get("HARNEST_HATCHET_CONSUMER_EVENTS")
    if not target:
        return
    event = {"component": "model_provider", "phase": phase}
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, separators=(",", ":")) + "\n")


@lifecycle.model.before
def before_provider(context, _request):
    """Prove a provider request crossed Harnest without retaining its prompt."""

    _record("before")
    return context.next()


@lifecycle.model.after
def after_provider(context, _response):
    """Prove a provider response returned without retaining its content."""

    _record("after")
    return context.next()
