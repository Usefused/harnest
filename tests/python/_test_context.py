"""Context-manager ownership for test fixtures on every supported Python version."""

from contextlib import AbstractContextManager, ExitStack
from typing import TypeVar
from unittest import TestCase


_T = TypeVar("_T")


def enter_context(test: TestCase, manager: AbstractContextManager[_T]) -> _T:
    """Register LIFO cleanup even if later setup fails, without Python 3.11 APIs."""
    stack = ExitStack()
    # One stack per manager preserves ordering with other addCleanup callbacks.
    test.addCleanup(stack.close)
    return stack.enter_context(manager)
