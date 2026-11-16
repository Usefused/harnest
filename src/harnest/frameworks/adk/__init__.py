"""Google ADK adapter and the ADK-native types authors use directly.

``ADKStore`` declares native ADK persistence in advanced mode, and
``ADKSessionStorage`` configures a deployment-owned ADK session backend. They
load lazily, so importing this package never imports ADK itself.
"""

from typing import TYPE_CHECKING

from ..lazy import lazy_exports

if TYPE_CHECKING:
    from .storage import ADKSessionStorage as ADKSessionStorage
    from .storage import ADKStore as ADKStore

_EXPORTS = {"ADKSessionStorage": ".storage", "ADKStore": ".storage"}

__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)

__all__ = ["ADKSessionStorage", "ADKStore"]
