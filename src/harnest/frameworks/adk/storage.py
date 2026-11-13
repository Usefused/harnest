"""ADK-native storage types: explicit native persistence and deployment sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ...checkpoint import CheckpointOwner, FrameworkStorage, _optional_async_call


@dataclass(frozen=True, slots=True)
class ADKSessionStorage:
    """Deployment-owned ADK session backend configuration."""

    uri: str
    database_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("ADK session storage uri must be non-empty")

    def create_service(self, base_directory: str) -> Any:
        """Create the native ADK session service for a compiled application."""

        from google.adk.cli.utils.service_factory import (
            create_session_service_from_options,
        )

        return create_session_service_from_options(
            base_dir=base_directory,
            session_service_uri=self.uri,
            session_db_kwargs=dict(self.database_kwargs),
            use_local_storage=False,
        )


class ADKStore(FrameworkStorage):
    """Explicit lifecycle ownership wrapper for native ADK persistence."""

    owner: CheckpointOwner = "adk"
    framework = "adk"
    schema_id = "adk-native"
    owns_sessions = True

    def __init__(self, session_service: Any) -> None:
        """Declare one native ADK service as both session and checkpoint authority."""

        if session_service is None:
            raise ValueError("ADKStore requires a native session service")
        self.session_service = session_service

    async def start(self) -> None:
        """Initialize the wrapped ADK session service when supported."""

        await _optional_async_call(self.session_service, "start")

    async def close(self) -> None:
        """Release resources owned by the wrapped ADK session service."""

        await _optional_async_call(self.session_service, "close")


__all__ = ["ADKSessionStorage", "ADKStore"]
