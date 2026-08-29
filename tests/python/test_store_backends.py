from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from harnest.checkpoint import (
    CheckpointConflictError,
    CheckpointRecord,
    CheckpointStore,
    MemoryStore,
    RunRecord,
    RunScope,
)
from harnest.continuation import (
    ContinuationRecord,
    ContinuationStore,
)
from harnest.session import SessionStore
from harnest.store import PostgresStore, RedisStore
from harnest.store_redis import _checkpoint_dump, _continuation_dump, _run_dump


class _Transaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Acquire(AbstractAsyncContextManager):
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def acquire(self):
        return _Acquire(self.connection)

    async def close(self):
        self.closed = True


class _PostgresConnection:
    def __init__(self):
        self.executed = []
        self.fetched = []
        self.rows = []
        self.schema_version = 4

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *arguments):
        self.executed.append((query, arguments))
        if "VALUES ('store', 4)" in query:
            self.schema_version = 4
        return "INSERT 0 1"

    async def fetchval(self, query, *arguments):
        self.fetched.append((query, arguments))
        return self.schema_version

    async def fetch(self, query, *arguments):
        self.fetched.append((query, arguments))
        return list(self.rows)

    async def fetchrow(self, query, *arguments):
        self.fetched.append((query, arguments))
        return self.rows.pop(0) if self.rows else None


class _PostgresCheckpointConnection(_PostgresConnection):
    def __init__(self, *, current_revision=None, next_revision=0):
        super().__init__()
        self.current_revision = current_revision
        self.next_revision = next_revision

    async def fetchval(self, query, *arguments):
        self.fetched.append((query, arguments))
        return self.next_revision

    async def fetchrow(self, query, *arguments):
        self.fetched.append((query, arguments))
        if "SELECT status" in query:
            return {"status": "running"}
        if "SELECT revision" in query:
            if self.current_revision is None:
                return None
            return {"revision": self.current_revision}
        if "INSERT INTO harnest_checkpoints" in query:
            return _checkpoint_row(arguments)
        return None


class _RedisClient:
    def __init__(self):
        self.values = {}
        self.evals = []
        self.session_ids = []
        self.multi_values = []
        self.mget_calls = 0
        self.zrangebylex_calls = []

    async def set(self, key, value, **options):
        if options.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def zrange(self, _key, _start, _end):
        return list(self.session_ids)

    async def zrangebylex(self, key, minimum, maximum, **options):
        self.zrangebylex_calls.append((key, minimum, maximum, options))
        values = list(self.session_ids)
        if minimum.startswith("("):
            boundary = minimum[1:].encode("utf-8")
            values = [item for item in values if item > boundary]
        limit = options.get("num")
        return values if limit is None else values[:limit]

    async def mget(self, keys):
        self.mget_calls += 1
        self.last_mget_keys = list(keys)
        return list(self.multi_values)

    async def eval(self, script, key_count, *values):
        self.evals.append((script, key_count, values))
        if "current_revision" in script:
            raw = values[key_count + 1]
            return [b"ok", raw]
        return 1


def _session_row(session_id="s-1"):
    now = datetime.now(timezone.utc)
    return {
        "session_id": session_id,
        "user_id": "u-1",
        "state": json.dumps({"count": 1}),
        "application_data": json.dumps({}),
        "created_at": now,
        "updated_at": now,
    }


def _checkpoint():
    return CheckpointRecord(
        run_id="run-1",
        checkpoint_id="cp-1",
        namespace="graph",
        framework="langgraph",
        type_name="json",
        payload=b"payload",
        metadata_type="json",
        metadata=b"metadata",
        versions_type="json",
        versions=b"versions",
    )


def _continuation_row(continuation_id="continuation-1"):
    now = datetime.now(timezone.utc)
    return {
        "continuation_id": continuation_id,
        "run_id": "run-1",
        "application_id": "support",
        "user_id": "u-1",
        "session_id": "s-1",
        "provider": "hatchet",
        "capability": "workflow.run",
        "schema_id": "result/v1",
        "resume": None,
        "external_id": "job-1",
        "external_key": "digest",
        "status": "pending",
        "revision": 0,
        "ready": False,
        "result": None,
        "failure": None,
        "created_at": now,
        "updated_at": now,
    }


def _continuation(continuation_id="continuation-1"):
    return ContinuationRecord(
        continuation_id=continuation_id,
        application_id="support",
        user_id="u-1",
        session_id="s-1",
        run_id="run-1",
        provider="hatchet",
        capability="workflow.run",
        schema_id="result/v1",
    )


def _scope():
    return RunScope("support", "u-1", "s-1", "run-1")


def _checkpoint_row(arguments):
    names = (
        "run_id",
        "namespace",
        "checkpoint_id",
        "framework",
        "type_name",
        "payload",
        "metadata_type",
        "metadata",
        "versions_type",
        "versions",
        "parent_checkpoint_id",
        "created_at",
        "revision",
    )
    return dict(zip(names, arguments))


class BuiltInStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_store_implements_both_storage_contracts(self):
        store = MemoryStore()
        self.assertIsInstance(store, SessionStore)
        self.assertIsInstance(store, CheckpointStore)
        self.assertIsInstance(store, ContinuationStore)
        await store.start()
        session = await store.create(session_id="s-1", user_id="u-1", state={})
        self.assertEqual(session.id, "s-1")
        await store.create(session_id="s-2", user_id="u-1", state={})
        await store.create(session_id="s-3", user_id="u-1", state={})
        page = await store.list(user_id="u-1", after="s-1", limit=1)
        self.assertEqual([item.id for item in page], ["s-2"])
        run = await store.begin_run(
            application_id="app",
            user_id="u-1",
            session_id="s-1",
            run_id="run-1",
            framework="langgraph",
        )
        self.assertEqual(run.status, "running")
        await store.close()

    async def test_production_stores_implement_both_contracts(self):
        postgres = PostgresStore("postgres://example", _pool=_Pool(_PostgresConnection()))
        redis = RedisStore("redis://example", _client=_RedisClient())
        for store in (postgres, redis):
            self.assertIsInstance(store, SessionStore)
            self.assertIsInstance(store, CheckpointStore)
            self.assertIsInstance(store, ContinuationStore)


class PostgresStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_lookup_applies_complete_ownership_scope(self):
        connection = _PostgresConnection()
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        self.assertIsNone(await store.get_run(scope=_scope()))

        query, arguments = connection.fetched[-1]
        self.assertIn("application_id=$2", query)
        self.assertIn("user_id=$3", query)
        self.assertIn("session_id=$4", query)
        self.assertEqual(arguments, ("run-1", "support", "u-1", "s-1"))

    async def test_start_bootstraps_schema_under_advisory_lock(self):
        connection = _PostgresConnection()
        store = PostgresStore("postgres://example", _pool=_Pool(connection))

        await store.start()

        statements = "\n".join(query for query, _ in connection.executed)
        self.assertIn("pg_advisory_xact_lock", statements)
        self.assertIn("harnest_schema_migrations", statements)
        self.assertIn("harnest_one_active_run", statements)
        self.assertIn("application_data", statements)
        self.assertIn("harnest_continuations", statements)
        self.assertIn("harnest_pending_continuations", statements)

    async def test_start_migrates_additive_postgres_schema_version_two(self):
        connection = _PostgresConnection()
        connection.schema_version = 2
        store = PostgresStore("postgres://example", _pool=_Pool(connection))

        await store.start()

        self.assertEqual(connection.schema_version, 4)
        statements = "\n".join(query for query, _ in connection.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS harnest_continuations", statements)

    async def test_session_list_filters_and_orders_in_postgres(self):
        connection = _PostgresConnection()
        connection.rows = [_session_row("a"), _session_row("b")]
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        sessions = await store.list(user_id="u-1")

        self.assertEqual([item.id for item in sessions], ["a", "b"])
        query, arguments = connection.fetched[-1]
        self.assertIn("WHERE user_id=$1", query)
        self.assertIn("session_id > $2", query)
        self.assertIn("ORDER BY session_id", query)
        self.assertIn("LIMIT $3", query)
        self.assertEqual(arguments, ("u-1", None, None))

        await store.list(user_id="u-1", after="a", limit=2)
        _, arguments = connection.fetched[-1]
        self.assertEqual(arguments, ("u-1", "a", 2))

    async def test_checkpoint_list_pushes_filter_cursor_and_limit_to_sql(self):
        connection = _PostgresConnection()
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        values = [
            value
            async for value in store.list_checkpoints(
                scope=_scope(), namespace="graph", before="cp-9", limit=7
            )
        ]

        self.assertEqual(values, [])
        query, arguments = connection.fetched[-1]
        self.assertIn("LIMIT $7", query)
        self.assertEqual(
            arguments,
            ("run-1", "support", "u-1", "s-1", "graph", "cp-9", 7),
        )

    async def test_checkpoint_put_serializes_global_revision_allocation(self):
        connection = _PostgresCheckpointConnection(next_revision=4)
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        stored = await store.put(
            _checkpoint(), scope=_scope(), expected_revision=None
        )

        self.assertEqual(stored.revision, 4)
        queries = "\n".join(query for query, _ in connection.fetched)
        self.assertIn("FOR UPDATE", queries)
        self.assertIn("MAX(revision)", queries)

    async def test_continuation_reconciliation_is_one_indexed_query(self):
        connection = _PostgresConnection()
        connection.rows = [_continuation_row()]
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        values = await store.list_pending_continuations(
            application_id="support", provider="hatchet", after="a", limit=5
        )

        self.assertEqual(values[0].external_id, "job-1")
        query, arguments = connection.fetched[-1]
        self.assertIn("application_id=$1 AND provider=$2", query)
        self.assertIn("status='pending'", query)
        self.assertIn("continuation_id > $3", query)
        self.assertIn("ORDER BY continuation_id", query)
        self.assertIn("LIMIT $4", query)
        self.assertEqual(arguments, ("support", "hatchet", "a", 5))

    async def test_checkpoint_put_rejects_stale_revision_before_write(self):
        connection = _PostgresCheckpointConnection(current_revision=3)
        store = PostgresStore(
            "postgres://example", _pool=_Pool(connection), setup_schema=False
        )

        with self.assertRaises(CheckpointConflictError):
            await store.put(
                _checkpoint(), scope=_scope(), expected_revision=2
            )

        self.assertFalse(
            any("INSERT INTO harnest_checkpoints" in query for query, _ in connection.fetched)
        )

    async def test_missing_asyncpg_has_actionable_error(self):
        store = PostgresStore("postgres://example")
        with patch(
            "harnest.store_postgres.importlib.import_module",
            side_effect=ModuleNotFoundError,
        ):
            with self.assertRaisesRegex(RuntimeError, r"harnest\[postgres\]"):
                await store.start()


class RedisStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_scope_cannot_read_or_delete_run(self):
        client = _RedisClient()
        store = RedisStore("redis://example", _client=client)
        client.values[store._run_key("run-1")] = _run_dump(
            RunRecord("support", "u-1", "s-1", "run-1", "langgraph")
        )
        foreign = RunScope("support", "other-user", "s-1", "run-1")

        self.assertIsNone(await store.get_run(scope=foreign))
        self.assertIsNone(await store.get_checkpoint(scope=foreign))
        await store.delete_run(scope=foreign)

        self.assertEqual(client.evals, [])
        self.assertIsNotNone(await store.get_run(scope=_scope()))

    async def test_start_sets_and_validates_schema_version(self):
        client = _RedisClient()
        store = RedisStore("redis://example", _client=client)

        await store.start()

        schema_keys = [key for key in client.values if key.endswith(":schema")]
        self.assertEqual(len(schema_keys), 1)
        self.assertEqual(client.values[schema_keys[0]], "3")

    async def test_start_migrates_additive_schema_version_one(self):
        client = _RedisClient()
        store = RedisStore("redis://example", _client=client)
        client.values[store._key("schema")] = "1"

        await store.start()

        self.assertEqual(client.values[store._key("schema")], "3")

    def test_atomic_keys_share_one_redis_cluster_slot(self):
        store = RedisStore("redis://example")
        keys = (
            store._session_key("user", "session"),
            store._run_key("run"),
            store._active_key("app", "user", "session"),
            store._checkpoint_index("run", "graph"),
        )

        slots = {key[key.index("{") : key.index("}") + 1] for key in keys}
        self.assertEqual(len(slots), 1)

    async def test_session_list_uses_one_set_based_mget(self):
        client = _RedisClient()
        client.session_ids = [b"a", b"b"]
        client.multi_values = [
            json.dumps(
                {
                    "id": name,
                    "user_id": "u-1",
                    "state": {},
                    "created_at": "now",
                    "updated_at": "now",
                }
            )
            for name in ("a", "b")
        ]
        store = RedisStore("redis://example", _client=client)

        sessions = await store.list(user_id="u-1")

        self.assertEqual([item.id for item in sessions], ["a", "b"])
        self.assertEqual(client.mget_calls, 1)
        self.assertTrue(all("u-1" not in key for key in client.last_mget_keys))

        client.session_ids = [b"a", b"b", b"c"]
        client.multi_values = [
            json.dumps(
                {
                    "id": "c",
                    "user_id": "u-1",
                    "state": {},
                    "created_at": "now",
                    "updated_at": "now",
                }
            )
        ]
        page = await store.list(user_id="u-1", after="b", limit=1)
        self.assertEqual([item.id for item in page], ["c"])
        _, minimum, maximum, options = client.zrangebylex_calls[-1]
        self.assertEqual((minimum, maximum), ("(b", "+"))
        self.assertEqual(options, {"start": 0, "num": 1})

    async def test_continuation_reconciliation_uses_one_batched_read(self):
        client = _RedisClient()
        client.session_ids = [b"continuation-1"]
        client.multi_values = [_continuation_dump(_continuation(), "job-1")]
        store = RedisStore("redis://example", _client=client)

        values = await store.list_pending_continuations(
            application_id="support", provider="hatchet", limit=5
        )

        self.assertEqual(values[0].external_id, "job-1")
        self.assertEqual(client.mget_calls, 1)
        self.assertEqual(len(client.zrangebylex_calls), 1)
        self.assertEqual(client.zrangebylex_calls[0][-1]["num"], 5)

    async def test_mutation_audit_excludes_tenant_identifiers_and_state(self):
        store = RedisStore("redis://example", _client=_RedisClient())

        with self.assertLogs("harnest.agent.store.audit", level="INFO") as logs:
            await store.create(
                session_id="secret-session",
                user_id="secret-user",
                state={"token": "secret-state"},
            )

        record = logs.records[-1]
        self.assertEqual(record.operation, "session.create")
        self.assertEqual(record.outcome, "committed")
        self.assertFalse(hasattr(record, "session_id"))
        self.assertFalse(hasattr(record, "user_id"))
        self.assertNotIn("secret-state", record.getMessage())

    async def test_checkpoint_put_round_trips_opaque_bytes(self):
        client = _RedisClient()
        store = RedisStore("redis://example", _client=client)
        client.values[store._run_key("run-1")] = _run_dump(
            RunRecord("support", "u-1", "s-1", "run-1", "langgraph")
        )

        stored = await store.put(
            _checkpoint(), scope=_scope(), expected_revision=None
        )

        self.assertEqual(stored.payload, b"payload")
        self.assertEqual(stored.metadata, b"metadata")
        self.assertEqual(stored.versions, b"versions")
        _script, key_count, values = client.evals[-1]
        self.assertEqual(key_count, 7)
        self.assertNotIn("run-1", " ".join(map(str, values[:key_count])))

    async def test_missing_redis_has_actionable_error(self):
        with patch(
            "harnest.store_redis.importlib.import_module",
            side_effect=ModuleNotFoundError,
        ):
            store = RedisStore("redis://example")
            with self.assertRaisesRegex(RuntimeError, r"harnest\[redis\]"):
                await store.start()

    def test_checkpoint_codec_is_binary_safe(self):
        encoded = _checkpoint_dump(_checkpoint())
        self.assertEqual(json.loads(encoded)["payload"], "cGF5bG9hZA==")


if __name__ == "__main__":
    unittest.main()
