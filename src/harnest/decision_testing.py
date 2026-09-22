"""Explicit offline decision fixtures using the same production validation path."""

from types import MappingProxyType
from typing import Mapping

from .decision_types import (
    DecisionCapabilities, DecisionRequest, DecisionResponse, identifier,
)


class FixtureDecisionProvider:
    """Return fixtures keyed by (decision name, version), without retaining private state."""

    def __init__(
        self, responses: Mapping[tuple[str, str], DecisionResponse], *,
        capabilities: DecisionCapabilities, version: str = "fixture-1",
    ) -> None:
        """Require explicit capabilities so fixtures cannot silently invent confidence."""
        identifier(version)
        if not isinstance(capabilities, DecisionCapabilities):
            raise TypeError("fixture capabilities must be DecisionCapabilities")
        self.capabilities = capabilities
        self.version = version
        self._responses = MappingProxyType(dict(responses))

    async def evaluate(self, request: DecisionRequest) -> DecisionResponse:
        """Select a deterministic fixture; missing revisions are errors, never live calls."""
        return self._responses[(request.definition.name, request.definition.version)]
