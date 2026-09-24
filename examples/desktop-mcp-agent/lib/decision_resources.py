"""Own the Jev client for the lifetime of the running desktop agent."""

from contextlib import asynccontextmanager
import os
from typing import AsyncIterator

from harnest.decisions import (
    DecisionAction, DecisionBinding, DecisionOutcome, Decisions,
)
from harnest.lib.browser_decision import BROWSER_POLICY, BROWSER_USE
from harnest.lib.jev import JevProvider


@asynccontextmanager
async def decision_registry() -> AsyncIterator[Decisions]:
    """Open Jev at runtime and fail clearly when its credential is absent."""
    key = os.environ.get("TYPESAFE_AI_KEY")
    if not key:
        raise RuntimeError("TYPESAFE_AI_KEY is required for the Jev browser decision")
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

    async with AsyncTypeSafeClient(
        api_key=key, timeout=5, retry=RetryPolicy(max_retries=0)
    ) as client:
        yield Decisions(
            providers={"jev": JevProvider(client)},
            bindings=(DecisionBinding(
                BROWSER_USE, "jev", BROWSER_POLICY,
                on_error=DecisionOutcome(DecisionAction.BLOCK),
            ),),
            timeout_seconds=6,
        )
