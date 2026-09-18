"""Explicit writes, isolated identity and revocable cross-session memory."""

import hashlib
import json
import struct
import unittest
from unittest.mock import AsyncMock, patch

from harnest import context
from harnest.context import activate_context, create_agent_context, derive_agent_context, revoke_context
from harnest.memory import InMemoryStore, MemoryRecord, MemoryScope, MemoryStorageError
from harnest.testing_memory import MemoryStoreConformanceMixin


def active_memory(store, *, user="alice", session="first", application="root"):
    """Construct the same trusted boundary used by managed invocation wrappers."""
    return create_agent_context(framework="langgraph", agent_name=application,
                                invocation_id="inv-" + session, user_id=user, session_id=session,
                                metadata={}, resources={}, memory_store=store)


class ReferenceMemoryTests(MemoryStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    async def make_store(self):
        """Exercise the public contract against the process-local reference."""
        return InMemoryStore()


class PostgresMemoryLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_has_explicit_python310_byte_order_and_stable_identity(self):
        """Require the 3.10 call signature while preserving existing replica lock IDs."""
        from harnest.memory_postgres import _locked_record

        def from_bytes(value, byteorder, *, signed=False):
            """Model Python 3.10, where byteorder has no default value."""
            return int.from_bytes(value, byteorder, signed=signed)

        scope = MemoryScope("memory-test", "alice")
        for key in ("preference", "other", "unicode-你好"):
            with self.subTest(key=key):
                connection = AsyncMock()
                connection.fetchval.return_value = None
                identity = (scope.application_id, scope.user_id, scope.namespace, key)
                digest = hashlib.sha256(json.dumps(identity).encode()).digest()[:8]
                expected = struct.unpack(">q", digest)[0]
                with patch("harnest.memory_postgres.int", create=True) as integer:
                    integer.from_bytes.side_effect = from_bytes
                    self.assertIsNone(await _locked_record(connection, scope, key))
                connection.execute.assert_awaited_once_with("SELECT pg_advisory_xact_lock($1)", expected)
                self.assertEqual(connection.fetchval.await_args.args[1:], identity)


class MemoryContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_metadata_is_not_silently_coerced(self):
        """Reject malformed writes before passing them to any custom provider."""
        active = active_memory(InMemoryStore())
        self.addCleanup(revoke_context, active)
        with activate_context(active):
            with self.assertRaises(TypeError):
                await context.memory.put("key", "text", metadata=[])
            with self.assertRaisesRegex(ValueError, "content must contain valid UTF-8"):
                await context.memory.put("key", "\ud800private")

    async def test_memory_crosses_sessions_but_not_users_or_namespaces(self):
        """Explicitly saved data is cross-session, not globally shared state."""
        store = InMemoryStore()
        first, second, other = active_memory(store), active_memory(store, session="second"), active_memory(store, user="bob")
        for active in (first, second, other):
            self.addCleanup(revoke_context, active)
        with activate_context(first):
            self.assertFalse(hasattr(context.memory, 'delete_all'))
            self.assertFalse(hasattr(context.memory, 'delete_user'))
            self.assertFalse(hasattr(context.memory, 'purge_expired'))
            saved = await context.memory.put("style", "Short reports")
        with activate_context(second):
            self.assertEqual((await context.memory.get("style")).content, "Short reports")
            self.assertIsNone(await context.memory.namespace("private").get("style"))
        with activate_context(other):
            self.assertIsNone(await context.memory.get("style"))
        self.assertEqual(saved.source["session_id"], "first")

    async def test_child_agents_share_root_scope_and_handles_cannot_transfer(self):
        """Derived agents preserve application identity without exporting live authority."""
        store = InMemoryStore()
        active, other = active_memory(store), active_memory(store, user="bob")
        self.addCleanup(revoke_context, active)
        self.addCleanup(revoke_context, other)
        with activate_context(active):
            handle = context.memory
            await handle.put("key", "value")
            child = derive_agent_context(active, agent_name="child")
            with activate_context(child):
                self.assertIsNotNone(await context.memory.get("key"))
        with activate_context(other), self.assertRaises(context.ContextUnavailableError):
            await handle.get("key")
        revoke_context(active)
        with self.assertRaises(context.ContextUnavailableError):
            await handle.get("key")

    async def test_custom_provider_errors_and_audit_never_expose_memory(self):
        """Audit successful writes once and sanitize unknown adapter exceptions."""
        store = InMemoryStore()
        active = active_memory(store)
        self.addCleanup(revoke_context, active)
        with activate_context(active), patch("harnest.memory_provider._AUDIT") as audit:
            await context.memory.put("secret-key", "private-content")
            self.assertEqual(audit.info.call_count, 1)
            self.assertNotIn("private-content", repr(audit.mock_calls))
            self.assertNotIn("secret-key", repr(audit.mock_calls))
            async def broken(*args):
                """Simulate a custom adapter exposing SQL parameters in its error."""
                raise RuntimeError("private-content")
            with patch.object(store, "get", broken):
                with self.assertRaisesRegex(MemoryStorageError, "^memory get failed$"):
                    await context.memory.get("secret-key")

    async def test_missing_storage_and_foreign_results_fail_closed(self):
        """Do not infer storage or accept a buggy adapter's foreign records."""
        active = active_memory(None)
        self.addCleanup(revoke_context, active)
        with activate_context(active), self.assertRaisesRegex(context.ContextResourceError, "storage.memory"):
            await context.memory.get("key")
        store = InMemoryStore()
        active = active_memory(store)
        self.addCleanup(revoke_context, active)
        async def foreign(*args):
            """Return another user's record to exercise the defensive boundary."""
            return MemoryRecord(MemoryScope("root", "bob"), "key", "private")
        with activate_context(active), patch.object(store, "get", foreign):
            with self.assertRaisesRegex(MemoryStorageError, "invalid scope"):
                await context.memory.get("key")

    async def test_revocation_during_read_does_not_return_content(self):
        """Copied task contexts cannot retain memory access after invocation exit."""
        store = InMemoryStore()
        active = active_memory(store)
        self.addCleanup(revoke_context, active)
        async def revoke_during_read(*args):
            """End authority while the provider is waiting on storage."""
            revoke_context(active)
            return None
        with activate_context(active), patch.object(store, "get", revoke_during_read):
            with self.assertRaises(context.ContextUnavailableError):
                await context.memory.get("key")
