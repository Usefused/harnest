"""Session-scoped Agent Plugin archive and routing contracts."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import hashlib
import io
import json
import stat
import unittest
from unittest.mock import patch
import zipfile

from harnest.agent_plugin_manifest import MCP_SCHEMA, PLUGIN_SCHEMA
from harnest.dynamic_agent_plugins import (
    DynamicAgentPluginError,
    DynamicAgentPluginRuntimeDriver,
)
from harnest.runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)


class _Driver:
    """Minimal backend proving session ownership and dynamic variant routing."""

    def __init__(self, marker: str = "base") -> None:
        self.info = AgentInfo("agent", "Agent", "test", {})
        self.marker = marker
        self.sessions: dict[tuple[str, str], SessionRecord] = {}
        self.closed = False
        self.forks: list[_Driver] = []

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, object]
    ) -> SessionRecord:
        record = SessionRecord(session_id, user_id, dict(state))
        self.sessions[(user_id, session_id)] = record
        return record

    @property
    def session_context_store(self) -> "_Driver":
        """Use the fake backend itself as restart-safe application-data storage."""

        return self

    @asynccontextmanager
    async def acquire(self, *, session_id: str, user_id: str):
        """Yield a minimal lease over one fake session record."""

        driver = self

        class Lease:
            async def replace_application_data(
                self, data: Mapping[str, object]
            ) -> SessionRecord:
                current = driver.sessions[(user_id, session_id)]
                updated = SessionRecord(
                    current.id,
                    current.user_id,
                    current.state,
                    application_data=dict(data),
                )
                driver.sessions[(user_id, session_id)] = updated
                return updated

        yield Lease()

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        return self.sessions.get((user_id, session_id))

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        values = [
            record
            for (owner, _), record in self.sessions.items()
            if owner == user_id and (after is None or record.id > after)
        ]
        values.sort(key=lambda record: record.id)
        return values if limit is None else values[:limit]

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        return () if (user_id, session_id) in self.sessions else None

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, object],
    ) -> SessionRecord | None:
        record = self.sessions.get((user_id, session_id))
        if record is None:
            return None
        updated = SessionRecord(record.id, user_id, {**record.state, **state_delta})
        self.sessions[(user_id, session_id)] = updated
        return updated

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        return self.sessions.pop((user_id, session_id), None) is not None

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            self.marker, (), None, request.session_id, request.metadata
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        yield {"type": "message", "text": self.marker}

    def fork_application(self, _application: object) -> "_Driver":
        child = _Driver("dynamic")
        self.forks.append(child)
        return child

    async def close(self) -> None:
        self.closed = True


def _zip(files: Mapping[str, str], *, symlink: str | None = None) -> bytes:
    """Build a deterministic in-memory plugin package for hostile archive tests."""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, value in files.items():
            package.writestr(name, value)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(info, "plugin.json")
    return output.getvalue()


def _archive(*, stdio: bool = False) -> bytes:
    """Create one valid package with a skill and selected MCP transport."""

    transport = (
        {"type": "stdio", "command": "python"}
        if stdio
        else {"type": "streamable-http", "url": "https://example.com/mcp"}
    )
    return _zip(
        {
            "plugin.json": json.dumps(
                {"$schema": PLUGIN_SCHEMA, "name": "session-plugin"}
            ),
            "mcp.json": json.dumps(
                {"$schema": MCP_SCHEMA, "mcpServers": {"remote": transport}}
            ),
            "skills/proof/SKILL.md": (
                "---\nname: proof\ndescription: Prove dynamic loading.\n---\n"
                "Use the remote proof tool.\n"
            ),
        }
    )


def _descriptor(archive: bytes, **changes: object) -> dict[str, object]:
    """Render the inline source shape accepted from Agent Desktop."""

    value: dict[str, object] = {
        "type": "inline",
        "name": "session-plugin",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "source": {
            "type": "base64",
            "media_type": "application/zip",
            "data": base64.b64encode(archive).decode("ascii"),
        },
    }
    value.update(changes)
    return value


class DynamicAgentPluginTests(unittest.IsolatedAsyncioTestCase):
    """Verify immutable package policy independently of provider SDKs."""

    async def asyncSetUp(self) -> None:
        self.backend = _Driver()
        self.driver = DynamicAgentPluginRuntimeDriver(self.backend, object())

    async def asyncTearDown(self) -> None:
        await self.driver.close()

    async def test_session_binds_name_and_digest_without_exposing_archive(self) -> None:
        archive = _archive()
        with self.assertLogs("harnest.agent.session.audit", level="INFO") as logs:
            created = await self.driver.create_session_with_plugins(
                session_id="session",
                user_id="user",
                state={},
                plugins=[_descriptor(archive)],
            )

        self.assertEqual(
            created.metadata["plugins"],
            [{"name": "session-plugin", "sha256": hashlib.sha256(archive).hexdigest()}],
        )
        self.assertNotIn("data", json.dumps(created.metadata))
        self.assertEqual([record.outcome for record in logs.records], ["committed"])
        self.assertNotIn("session-plugin", logs.output[0])
        fetched = await self.driver.get_session(session_id="session", user_id="user")
        self.assertEqual(fetched, created)

    async def test_dynamic_session_routes_through_one_cached_backend_variant(self) -> None:
        archive = _archive()
        await self.driver.create_session_with_plugins(
            session_id="session",
            user_id="user",
            state={},
            plugins=[_descriptor(archive)],
        )
        request = InvocationRequest("hello", "user", "session", "run", {}, {})

        with patch(
            "harnest.dynamic_agent_plugins._plugin_application",
            return_value=object(),
        ):
            first = await self.driver.invoke(request)
            second = await self.driver.invoke(request)

        self.assertEqual((first.text, second.text), ("dynamic", "dynamic"))
        self.assertEqual(len(self.backend.forks), 1)

    async def test_persisted_snapshot_recovers_after_runtime_restart(self) -> None:
        archive = _archive()
        await self.driver.create_session_with_plugins(
            session_id="durable",
            user_id="user",
            state={},
            plugins=[_descriptor(archive)],
        )
        restarted_backend = _Driver()
        restarted_backend.sessions = self.backend.sessions
        restarted = DynamicAgentPluginRuntimeDriver(restarted_backend, object())
        self.addAsyncCleanup(restarted.close)

        fetched = await restarted.get_session(
            session_id="durable", user_id="user"
        )

        self.assertEqual(fetched.metadata["plugins"][0]["name"], "session-plugin")
        self.assertEqual(fetched.application_data, {})

    async def test_dynamic_stdio_is_rejected_before_session_creation(self) -> None:
        archive = _archive(stdio=True)
        with self.assertRaisesRegex(DynamicAgentPluginError, "sandbox"):
            await self.driver.create_session_with_plugins(
                session_id="session",
                user_id="user",
                state={},
                plugins=[_descriptor(archive)],
            )
        self.assertEqual(self.backend.sessions, {})

    async def test_digest_name_and_url_sources_fail_closed(self) -> None:
        archive = _archive()
        invalid = [
            _descriptor(archive, sha256="0" * 64),
            _descriptor(archive, name="different"),
            {"type": "url", "name": "session-plugin", "source": "https://example.com"},
        ]
        for index, descriptor in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(DynamicAgentPluginError):
                await self.driver.create_session_with_plugins(
                    session_id=f"session-{index}",
                    user_id="user",
                    state={},
                    plugins=[descriptor],
                )

    async def test_traversal_and_symlink_members_are_rejected(self) -> None:
        manifest = json.dumps({"$schema": PLUGIN_SCHEMA, "name": "session-plugin"})
        archives = [
            _zip({"plugin.json": manifest, "../outside": "private"}),
            _zip({"plugin.json": manifest}, symlink="linked"),
        ]
        for index, archive in enumerate(archives):
            with self.subTest(index=index), self.assertRaises(DynamicAgentPluginError):
                await self.driver.create_session_with_plugins(
                    session_id=f"unsafe-{index}",
                    user_id="user",
                    state={},
                    plugins=[_descriptor(archive)],
                )


if __name__ == "__main__":
    unittest.main()
