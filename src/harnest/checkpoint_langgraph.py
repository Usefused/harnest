"""LangGraph's native saver interface backed by a Harnest CheckpointStore."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from .checkpoint import CheckpointRecord, CheckpointStore, CheckpointWrite


class HarnestCheckpointSaver(BaseCheckpointSaver[Any]):
    """Persist native LangGraph payloads under one Harnest invocation run."""

    def __init__(self, store: CheckpointStore) -> None:
        """Bind one portable store to LangGraph's native saver contract."""

        if not isinstance(store, CheckpointStore):
            raise TypeError("store must implement CheckpointStore")
        super().__init__()
        self.store = store

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        """Rehydrate one native checkpoint and its pending writes."""

        identity = _identity(config)
        record = await self.store.get_checkpoint(
            run_id=identity.run_id,
            checkpoint_id=identity.checkpoint_id,
            namespace=identity.namespace,
        )
        if record is None:
            return None
        writes = await self.store.get_writes(
            run_id=record.run_id, checkpoint_id=record.checkpoint_id
        )
        return _checkpoint_tuple(self, record, writes)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List bounded history with one batched pending-write read."""

        if filter:
            raise ValueError("managed checkpoints do not support metadata filtering")
        if config is None:
            raise ValueError("checkpoint listing requires a run configuration")
        identity = _identity(config)
        before_id = _identity(before).checkpoint_id if before is not None else None
        records = [
            record
            async for record in self.store.list_checkpoints(
                run_id=identity.run_id,
                namespace=identity.namespace,
                before=before_id,
                limit=limit or 20,
            )
        ]
        writes = await self.store.get_writes_batch(
            run_id=identity.run_id,
            checkpoint_ids=tuple(item.checkpoint_id for item in records),
        )
        for record in records:
            yield _checkpoint_tuple(
                self, record, writes.get(record.checkpoint_id, ())
            )

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Serialize and persist one native checkpoint through portable CAS."""

        identity = _identity(config)
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("LangGraph checkpoint must contain a non-empty id")
        parent_id = identity.checkpoint_id
        checkpoint_value = _dump(self, checkpoint)
        metadata_value = _dump(self, metadata)
        versions_value = _dump(self, dict(new_versions))
        record = CheckpointRecord(
            run_id=identity.run_id,
            checkpoint_id=checkpoint_id,
            namespace=identity.namespace,
            framework="langgraph",
            type_name=checkpoint_value[0],
            payload=checkpoint_value[1],
            metadata_type=metadata_value[0],
            metadata=metadata_value[1],
            versions_type=versions_value[0],
            versions=versions_value[1],
            parent_checkpoint_id=parent_id,
        )
        await self.store.put(record, expected_revision=None)
        return _config(identity.run_id, identity.namespace, checkpoint_id)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Serialize native pending writes without exposing their payloads."""

        identity = _identity(config, require_checkpoint=True)
        encoded = tuple(
            _checkpoint_write(self, task_id, task_path, channel, value)
            for channel, value in writes
        )
        await self.store.put_writes(
            run_id=identity.run_id,
            checkpoint_id=identity.checkpoint_id or "",
            writes=encoded,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """Map LangGraph thread deletion to private run cleanup."""

        await self.store.delete_run(run_id=thread_id)


class _Identity:
    def __init__(self, run_id: str, namespace: str, checkpoint_id: str | None) -> None:
        self.run_id = run_id
        self.namespace = namespace
        self.checkpoint_id = checkpoint_id


def _identity(
    config: Mapping[str, Any] | None, *, require_checkpoint: bool = False
) -> _Identity:
    """Validate the native config fields used as portable checkpoint identity."""

    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(configurable, Mapping):
        raise ValueError("LangGraph checkpoint config requires configurable values")
    run_id = configurable.get("thread_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("LangGraph checkpoint config requires thread_id")
    namespace = configurable.get("checkpoint_ns", "")
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(namespace, str):
        raise TypeError("checkpoint_ns must be a string")
    if checkpoint_id is not None and not isinstance(checkpoint_id, str):
        raise TypeError("checkpoint_id must be a string")
    if require_checkpoint and not checkpoint_id:
        raise ValueError("checkpoint writes require checkpoint_id")
    return _Identity(run_id, namespace, checkpoint_id)


def _dump(saver: HarnestCheckpointSaver, value: Any) -> tuple[str, bytes]:
    return saver.serde.dumps_typed(value)


def _checkpoint_write(
    saver: HarnestCheckpointSaver,
    task_id: str,
    task_path: str,
    channel: str,
    value: Any,
) -> CheckpointWrite:
    type_name, payload = _dump(saver, value)
    return CheckpointWrite(task_id, channel, type_name, payload, task_path)


def _load(saver: HarnestCheckpointSaver, type_name: str, payload: bytes) -> Any:
    return saver.serde.loads_typed((type_name, payload))


def _config(run_id: str, namespace: str, checkpoint_id: str) -> dict[str, Any]:
    """Keep portable run identity compatible with LangGraph's config contract."""

    return {
        "configurable": {
            "thread_id": run_id,
            "checkpoint_ns": namespace,
            "checkpoint_id": checkpoint_id,
        }
    }


def _checkpoint_tuple(
    saver: HarnestCheckpointSaver,
    record: CheckpointRecord,
    writes: Sequence[CheckpointWrite],
) -> CheckpointTuple:
    """Reconstruct LangGraph's native tuple without interpreting opaque state."""

    config = _config(record.run_id, record.namespace, record.checkpoint_id)
    parent = (
        _config(record.run_id, record.namespace, record.parent_checkpoint_id)
        if record.parent_checkpoint_id
        else None
    )
    pending = [
        (item.task_id, item.channel, _load(saver, item.type_name, item.payload))
        for item in writes
    ]
    return CheckpointTuple(
        config=config,
        checkpoint=_load(saver, record.type_name, record.payload),
        metadata=_load(saver, record.metadata_type, record.metadata),
        parent_config=parent,
        pending_writes=pending,
    )


__all__ = ["HarnestCheckpointSaver"]
