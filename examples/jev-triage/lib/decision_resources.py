"""Runtime-owned Jev connections with an explicit, isolated offline fixture mode."""

from contextlib import asynccontextmanager
import os
from typing import AsyncIterator

from harnest.decisions import (
    ChoiceResult, DecisionAction, DecisionBinding, DecisionOutcome,
    DecisionResponse, Decisions, FixtureDecisionProvider,
)
from harnest.lib.definitions import TRIAGE, ROUTING
from harnest.lib.jev import JevProvider


def offline_enabled() -> bool:
    """Require explicit fixture mode rather than falling back after live failures."""
    value = os.environ.get("JEV_TRIAGE_OFFLINE", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("JEV_TRIAGE_OFFLINE must be true or false")
    return value == "true"


def offline_registry() -> Decisions:
    """Return a labeled fixed billing result without importing a network SDK."""
    fixture = FixtureDecisionProvider(
        {("support_route", "1"): DecisionResponse({
            "department": ChoiceResult(
                "billing", {"billing": 0.95, "support": 0.03, "other": 0.02}, 0.95,
            ),
        })},
        capabilities=JevProvider.capabilities,
    )
    return Decisions(
        providers={"fixture": fixture},
        bindings=(DecisionBinding(TRIAGE, "fixture", ROUTING),),
    )


@asynccontextmanager
async def decision_registry() -> AsyncIterator[Decisions]:
    """Open the SDK only at live runtime startup and close it on every exit path."""
    if offline_enabled():
        yield offline_registry()
        return

    # Optional provider imports and credentials stay out of compilation and fixtures.
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

    # Preserve the SDK's standard name while accepting the example's shared key.
    api_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_AI_KEY")
    async with AsyncTypeSafeClient(
        api_key=api_key, timeout=5, retry=RetryPolicy(max_retries=0),
    ) as client:
        yield Decisions(
            providers={"jev": JevProvider(client)},
            bindings=(DecisionBinding(
                TRIAGE, "jev", ROUTING,
                on_error=DecisionOutcome(DecisionAction.REVIEW),
            ),),
            timeout_seconds=6,
        )
