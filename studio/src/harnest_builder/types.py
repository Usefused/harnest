"""Shared contracts for embedding Studio without depending on its route internals."""

from collections.abc import Awaitable, Sequence
from typing import Protocol


class CompletionMessage(Protocol):
    """The text portion of a provider's completed response."""

    @property
    def content(self) -> str | None:
        """Return provider text, or no text for an empty completion."""
        ...


class CompletionChoice(Protocol):
    """One provider response choice consumed by the proposal boundary."""

    @property
    def message(self) -> CompletionMessage:
        """Expose the selected message without requiring a particular provider SDK."""
        ...


class CompletionResponse(Protocol):
    """Provider-neutral response accepted by Studio's completion hook."""

    @property
    def choices(self) -> Sequence[CompletionChoice]:
        """Expose completion choices in provider order."""
        ...


class Completion(Protocol):
    """Async model hook; endpoint credentials arrive as optional keyword arguments."""

    def __call__(self, *, model: str, messages: list[dict[str, str]],
                 timeout: int, max_tokens: int, **options: str) -> Awaitable[CompletionResponse]:
        """Produce a provider response for a bounded source proposal."""
        ...
