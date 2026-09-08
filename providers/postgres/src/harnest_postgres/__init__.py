"""PostgreSQL storage for Harnest, sharing one managed connection pool."""

from harnest.store_postgres import PostgresStore as _SessionStore
from harnest.task_store_postgres import PostgresTaskStore


class PostgresStore(_SessionStore, PostgresTaskStore):
    """Store sessions, checkpoints, continuations, tasks and cron together."""

    async def start(self) -> None:
        """Provision both storage contracts using the combined store's pool."""

        await _SessionStore.start(self)
        await self._start_task_storage()


__all__ = ["PostgresStore", "PostgresTaskStore"]
