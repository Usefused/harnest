"""LangGraph adapter and framework-specific types for advanced applications.

``LangGraphStore`` declares a LangGraph-owned saver in advanced mode.
Harnest-owned stores expose ``store.framework_checkpointer()`` instead.
"""

from typing import TYPE_CHECKING

from ..lazy import lazy_exports

if TYPE_CHECKING:
    from .storage import LangGraphStore as LangGraphStore

_EXPORTS = {"LangGraphStore": ".storage"}

__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)

__all__ = ["LangGraphStore"]
