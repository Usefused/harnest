"""Load the checked-in extension with the same package name as its wheel."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
import sys


_ROOT = Path(__file__).parents[1]
_PACKAGE = "harnest_extension_docker"

if _PACKAGE not in sys.modules:
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    package.__package__ = _PACKAGE
    sys.modules[_PACKAGE] = package
