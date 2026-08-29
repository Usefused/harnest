"""Invocation-scoped access to explicitly named custom storage resources."""

from __future__ import annotations

import re
from typing import Any

from .context import ContextResourceError, context


_STORAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")


class StorageContext:
    """Resolve custom storage without exposing sessions or checkpoints."""

    def __call__(
        self, name: str, expected_type: type[Any] | None = None
    ) -> Any:
        """Return one named custom resource for concise authored access."""

        return self.resource(name, expected_type)

    def resource(
        self, name: str, expected_type: type[Any] | None = None
    ) -> Any:
        """Resolve a custom store while keeping the registry non-enumerable."""

        _validate_storage_name(name)
        active = context.current()
        value = active._custom_stores.get(name)
        if value is None:
            raise ContextResourceError(
                f"custom storage {name!r} is not available in this invocation"
            )
        if expected_type is not None and not isinstance(value, expected_type):
            raise ContextResourceError(
                f"custom storage {name!r} must be {expected_type.__name__}; "
                f"got {type(value).__name__}"
            )
        return value


storage = StorageContext()


def _validate_storage_name(name: Any) -> None:
    """Keep authored lookup names identical to lifecycle registration names."""

    if not isinstance(name, str) or not _STORAGE_NAME.fullmatch(name):
        raise ValueError("custom storage name must be a valid storage identifier")


__all__ = ["StorageContext", "storage"]
