"""Explicit control-flow values shared by portable lifecycle stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar, overload


ValueT = TypeVar("ValueT")
ResultT = TypeVar("ResultT")


class _Unchanged:
    """Represent continuation without conflating it with a replacement of ``None``."""

    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED = _Unchanged()


@dataclass(frozen=True, slots=True)
class Next(Generic[ValueT]):
    """Continue a lifecycle chain, optionally with a replacement value."""

    value: ValueT | _Unchanged = UNCHANGED

    @property
    def replaces(self) -> bool:
        """Report whether the next stage should receive a replacement value."""

        return self.value is not UNCHANGED


@dataclass(frozen=True, slots=True)
class Finish(Generic[ResultT]):
    """Complete an intercepting lifecycle stage without calling later work."""

    result: ResultT


class TransitionContext:
    """Give typed lifecycle contexts one consistent control-flow vocabulary."""

    @overload
    def next(self) -> Next[Any]: ...

    @overload
    def next(self, replacement: ValueT) -> Next[ValueT]: ...

    def next(self, replacement: Any = UNCHANGED) -> Next[Any]:
        """Continue normally or pass a replacement to the next interceptor."""

        return Next(replacement)

    def finish(self, result: ResultT) -> Finish[ResultT]:
        """Return a final value without advancing to later execution."""

        return Finish(result)


def is_transition(value: Any) -> bool:
    """Return whether a callback produced explicit portable control flow."""

    return isinstance(value, (Next, Finish))


__all__ = ["Finish", "Next", "TransitionContext", "UNCHANGED", "is_transition"]
