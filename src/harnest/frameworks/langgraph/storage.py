"""LangGraph-native storage types authors use in advanced mode."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from ...checkpoint import CheckpointOwner, HarnestStore, FrameworkStorage, _optional_async_call

try:
    from langgraph.checkpoint.base import BaseCheckpointSaver as _LangGraphSaverBase
except ImportError:  # Public exports must import without LangGraph installed.
    class _LangGraphSaverBase:  # type: ignore[no-redef]
        """Stand-in base so the type is importable where LangGraph is absent."""

        def __init__(self, *, serde: Any = None) -> None:
            """Keep LangGraph's serializer attribute shape."""

            self.serde = serde


class LangGraphStore(FrameworkStorage, _LangGraphSaverBase):
    """Explicit lifecycle ownership wrapper for a native LangGraph saver."""

    owner: CheckpointOwner = "langgraph"
    framework = "langgraph"
    schema_id = "langgraph-native"

    def __init__(self, checkpointer: Any) -> None:
        """Wrap the exact saver used by an advanced compiled graph."""

        if checkpointer is None:
            raise ValueError("LangGraphStore requires a native checkpointer")
        self.checkpointer = checkpointer

        # LangGraph validates the lifecycle value with isinstance before it can
        # use delegation, so the wrapper implements its native saver base class.
        _LangGraphSaverBase.__init__(
            self, serde=getattr(checkpointer, "serde", None)
        )

    @property
    def config_specs(self) -> list[Any]:
        """Expose the wrapped saver configuration fields to LangGraph."""

        return self.checkpointer.config_specs

    def get_tuple(self, config: Any) -> Any:
        """Delegate synchronous checkpoint lookup to the wrapped saver."""

        return self.checkpointer.get_tuple(config)

    def list(self, config: Any, **options: Any) -> Any:
        """Delegate synchronous checkpoint listing to the wrapped saver."""

        return self.checkpointer.list(config, **options)

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Delegate synchronous checkpoint persistence to the wrapped saver."""

        return self.checkpointer.put(
            config, checkpoint, metadata, new_versions
        )

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Delegate synchronous pending-write persistence to the wrapped saver."""

        self.checkpointer.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints for a LangGraph thread."""

        self.checkpointer.delete_thread(thread_id)

    async def aget_tuple(self, config: Any) -> Any:
        """Delegate asynchronous checkpoint lookup to the wrapped saver."""

        return await self.checkpointer.aget_tuple(config)

    async def alist(self, config: Any, **options: Any) -> AsyncIterator[Any]:
        """Stream checkpoints from the wrapped asynchronous saver."""

        async for value in self.checkpointer.alist(config, **options):
            yield value

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Delegate asynchronous checkpoint persistence to the wrapped saver."""

        return await self.checkpointer.aput(
            config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Delegate asynchronous pending-write persistence to the wrapped saver."""

        await self.checkpointer.aput_writes(
            config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """Asynchronously delete all checkpoints for a LangGraph thread."""

        await self.checkpointer.adelete_thread(thread_id)

    def get_next_version(self, current: Any, channel: Any) -> Any:
        """Ask the wrapped saver to derive the next channel version."""

        return self.checkpointer.get_next_version(current, channel)

    def __getattr__(self, name: str) -> Any:
        # Delegation makes the lifecycle-owned wrapper usable directly in
        # builder.compile(checkpointer=...) without hiding its ownership.
        return getattr(self.checkpointer, name)

    async def start(self) -> None:
        """Initialize the wrapped saver when it owns a start hook."""

        await _optional_async_call(self.checkpointer, "start")

    async def close(self) -> None:
        """Release resources owned by the wrapped saver."""

        await _optional_async_call(self.checkpointer, "close")

def _checkpointer(store: HarnestStore) -> Any:
    """Return one stable LangGraph saver backed by a Harnest store.

    Use it when compiling an advanced LangGraph target whose checkpoints
    Harnest owns: ``graph.compile(checkpointer=store.framework_checkpointer())``.
    """

    if not isinstance(store, HarnestStore):
        raise TypeError("framework checkpointers require a HarnestStore")
    saver = getattr(store, "_harnest_langgraph_saver", None)
    if saver is None:
        from .checkpoint import HarnestCheckpointSaver

        saver = HarnestCheckpointSaver(store)
        setattr(store, "_harnest_langgraph_saver", saver)
    return saver


__all__ = ["LangGraphStore"]
