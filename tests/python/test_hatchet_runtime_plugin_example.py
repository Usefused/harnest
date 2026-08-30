from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from harnest.continuation import ContinuationConflictError
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.plugins import (
    Plugin,
    PluginContext,
    activate_runtime_plugins,
    release_runtime_plugins,
)
from harnest.runtime_plugins import discover_runtime_plugins


_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "plugins"
    / "hatchet"
)


class _Suspended:
    def __init__(self) -> None:
        self.future: asyncio.Future[Mapping[str, object]] = (
            asyncio.get_running_loop().create_future()
        )

    async def result(self) -> Mapping[str, object]:
        """Wait for the fake provider port to complete this continuation."""

        return await self.future


class _InvocationContinuations:
    def __init__(self) -> None:
        self.suspended = _Suspended()
        self.external_id: str | None = None
        self.capability: str | None = None
        self.schema_id: str | None = None
        self.validate = None

    async def suspend(
        self, external_id, *, capability, schema_id, validate
    ) -> _Suspended:
        """Capture provider ownership and validation passed by the plugin."""

        self.external_id = external_id
        self.capability = capability
        self.schema_id = schema_id
        self.validate = validate
        return self.suspended


class _ProviderContinuations:
    def __init__(self, invocation: _InvocationContinuations) -> None:
        self.invocation = invocation
        self.completed: list[tuple[str, Mapping[str, object]]] = []
        self.failed: list[tuple[str, str]] = []
        self.schemas = {}
        self.pending = []
        self.page_cap = 100
        self.list_calls = []

    def register_schema(self, schema_id, validate) -> None:
        """Retain the restart-stable validator registered during plugin startup."""

        self.schemas[schema_id] = validate

    async def complete(self, external_id, result) -> None:
        """Validate and resolve once like the provider-neutral runtime port."""

        validate = self.invocation.validate or self.schemas["hatchet.run-result.v1"]
        validated = validate(result)
        self.completed.append((external_id, validated))
        if not self.invocation.suspended.future.done():
            self.invocation.suspended.future.set_result(validated)

    async def fail(self, external_id, error_code) -> None:
        """Record only the stable provider failure code."""

        self.failed.append((external_id, error_code))
        if not self.invocation.suspended.future.done():
            self.invocation.suspended.future.set_exception(RuntimeError(error_code))

    async def list_pending(self, *, after=None, limit=100):
        """Apply keyset pagination before returning one bounded fake page."""

        self.list_calls.append((after, limit))
        candidates = [
            item
            for item in self.pending
            if after is None or item.record.continuation_id > after
        ]
        return tuple(candidates[: min(limit, self.page_cap)])


class _Transport:
    def __init__(self, module, statuses=()) -> None:
        self.module = module
        self.statuses = list(statuses)
        self.closed = 0
        self.cancelled = 0
        self.run_calls = []

    async def run(
        self,
        workflow_name,
        job_input,
        *,
        correlation_id,
        idempotency_key=None,
    ):
        """Return provider identity without retaining the supplied payload."""

        self.run_calls.append(
            (workflow_name, dict(job_input), correlation_id, idempotency_key)
        )
        return self.module.HatchetRun("run-1", workflow_name, correlation_id)

    async def status(self, job):
        """Return deterministic states for the plugin-owned monitor."""

        del job
        return self.statuses.pop(0)

    async def result(self, job):
        """Return the external worker's terminal workflow result."""

        del job
        return {"report": "ready"}

    async def cancel(self, job) -> None:
        """Record a provider request without coupling it to plugin shutdown."""

        del job
        self.cancelled += 1

    async def aclose(self) -> None:
        """Record release of client-owned resources."""

        self.closed += 1


class HatchetRuntimePluginExampleTests(unittest.IsolatedAsyncioTestCase):
    def test_plugin_contributes_no_agent_tools(self):
        """Keep domain tool ownership in the consuming agent."""

        self.assertFalse((_SOURCE / "tools").exists())

    async def test_public_api_submits_and_resumes_through_provider_ports(self):
        """Exercise the reusable plugin contract without a fake Hatchet server."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, plugin):
            create_transport = _Transport(module)
            monitor_transport = _Transport(
                module,
                (module.HatchetRunStatus.RUNNING, module.HatchetRunStatus.COMPLETED),
            )
            transports = iter((create_transport, monitor_transport))
            audit = Mock()
            active = _agent_context()
            try:
                with (
                    activate_context(active),
                    patch.object(
                        module,
                        "open_hatchet_transport",
                        side_effect=lambda _scopes: next(transports),
                    ),
                    patch.object(
                        module, "invocation_continuations", return_value=invocation
                    ),
                    patch.object(module.asyncio, "sleep", AsyncMock()),
                    patch(
                        "harnest.plugin_runtime_context._AUDIT",
                        SimpleNamespace(info=audit),
                    ),
                ):
                    job = await module.hatchet.run(
                        "consumer-report", {"topic": "plugin test"}
                    )
                    result = await module.hatchet.wait(job)
            finally:
                revoke_context(active)

            self.assertEqual(job.run_id, "run-1")
            self.assertEqual(job.correlation_id, "invoke-1")
            self.assertEqual(
                create_transport.run_calls,
                [
                    (
                        "consumer-report",
                        {"topic": "plugin test"},
                        "invoke-1",
                        None,
                    )
                ],
            )
            self.assertEqual(result, {"report": "ready"})
            self.assertEqual(invocation.external_id, "run-1")
            self.assertEqual(invocation.capability, "hatchet.run")
            self.assertEqual(invocation.schema_id, "hatchet.run-result.v1")
            self.assertEqual(provider.failed, [])
            self.assertEqual(provider.completed[0][0], "run-1")
            self.assertEqual(create_transport.closed, 1)
            self.assertEqual(monitor_transport.closed, 1)
            audit_operations = [
                call.kwargs["operation"] for call in audit.call_args_list
            ]
            self.assertEqual(
                audit_operations, ["run.create", "continuation.complete"]
            )
            self.assertNotIn("plugin test", repr(audit.call_args_list))

    async def test_cancel_is_explicit_and_plugin_stop_does_not_cancel_jobs(self):
        """Prove external runtime ownership is independent from plugin lifetime."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, plugin):
            transport = _Transport(module)
            active = _agent_context()
            job = module.HatchetRun("run-2", "consumer-report", "invoke-1")
            try:
                with (
                    activate_context(active),
                    patch.object(
                        module,
                        "open_hatchet_transport",
                        return_value=transport,
                    ),
                ):
                    await module.hatchet.cancel(job)
            finally:
                revoke_context(active)

            self.assertEqual(transport.cancelled, 1)
            await plugin.stop()
            self.assertEqual(transport.cancelled, 1)

    async def test_new_replica_recovers_pending_runs_through_bounded_pages(self):
        """Stop one replica and let startup reconciliation restore its monitors."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider):
            pass

        provider.pending = [
            _pending("continuation-a", "run-a", "invocation-a"),
            _pending("continuation-b", "run-b", "invocation-b"),
            _pending("continuation-c", "run-c", "invocation-c"),
        ]
        provider.page_cap = 2
        fixture = self._active_plugin(provider)
        async with fixture as (module, plugin):
            transports = [
                _Transport(module, (module.HatchetRunStatus.COMPLETED,))
                for _ in provider.pending
            ]
            opened = iter(transports)
            audit = Mock()
            with (
                patch.object(
                    module,
                    "open_hatchet_recovery_transport",
                    side_effect=lambda: next(opened),
                ),
                patch(
                    "harnest.plugin_runtime_context._AUDIT",
                    SimpleNamespace(info=audit),
                ),
            ):
                await asyncio.gather(*tuple(plugin._monitors.values()))

        self.assertEqual(
            provider.list_calls[-3:],
            [(None, 100), ("continuation-b", 100), ("continuation-c", 100)],
        )
        self.assertEqual(
            [external_id for external_id, _result in provider.completed],
            ["run-a", "run-b", "run-c"],
        )
        self.assertTrue(all(transport.closed == 1 for transport in transports))
        self.assertEqual(
            [call.kwargs["operation"] for call in audit.call_args_list],
            ["continuation.complete"] * 3,
        )

    async def test_startup_recovery_failure_never_exposes_service_token(self):
        """Reduce application credential failures to a stable continuation code."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        provider.pending = [_pending("continuation-a", "run-a", "invocation-a")]
        fixture = self._active_plugin(provider)
        async with fixture as (module, plugin):
            with patch.object(
                module,
                "open_hatchet_recovery_transport",
                side_effect=RuntimeError("private-service-token"),
            ):
                await asyncio.gather(*tuple(plugin._monitors.values()))

        self.assertEqual(provider.failed, [("run-a", "provider_unavailable")])
        self.assertNotIn("private-service-token", repr(provider.failed))
        with self.assertRaisesRegex(RuntimeError, "provider_unavailable"):
            await invocation.suspended.result()

    async def test_replica_losing_completion_cas_exits_without_failing_wait(self):
        """Treat another replica's completion as successful monitor convergence."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, plugin):
            transport = _Transport(
                module, (module.HatchetRunStatus.COMPLETED,)
            )
            provider.complete = AsyncMock(
                side_effect=ContinuationConflictError(
                    "continuation state changed"
                )
            )
            job = module.HatchetRun("run-a", "report", "invocation-a")
            await plugin._monitor(job, transport, provider)

        self.assertEqual(provider.failed, [])
        self.assertEqual(transport.closed, 1)

    async def test_sdk_failures_are_detached_from_secret_messages(self):
        """Keep provider exception content out of the plugin error boundary."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, _plugin):
            client_module = __import__(
                f"{module.__name__}.lib.client", fromlist=["HatchetSDKTransport"]
            )

            class _Runs:
                async def aio_get_status(self, _run_id):
                    raise RuntimeError("private-token-value")

            transport = object.__new__(client_module.HatchetSDKTransport)
            transport._client = SimpleNamespace(runs=_Runs())
            job = client_module.HatchetRun("run-3", "report", "invoke-1")

            with self.assertRaises(client_module.HatchetPluginError) as captured:
                await transport.status(job)

            self.assertIn("RuntimeError", str(captured.exception))
            self.assertNotIn("private-token-value", str(captured.exception))
            self.assertIsNone(captured.exception.__context__)

    async def test_sdk_idempotent_submit_uses_public_workflow_stub(self):
        """Keep durable replay on the pinned SDK's supported stub surface."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, _plugin):
            client_module = __import__(
                f"{module.__name__}.lib.client", fromlist=["HatchetSDKTransport"]
            )
            calls = []

            class _Workflow:
                async def aio_run(self, value, *, options, wait_for_result):
                    calls.append((value, options))
                    self.wait_for_result = wait_for_result
                    return SimpleNamespace(workflow_run_id="durable-run")

            class _Stubs:
                def workflow(self, *, name, input_validator):
                    calls.append((name, input_validator))
                    self.last_workflow = _Workflow()
                    return self.last_workflow

            transport = object.__new__(client_module.HatchetSDKTransport)
            transport._client = SimpleNamespace(stubs=_Stubs())
            run_id = await transport._submit_idempotent(
                "consumer-report",
                {"topic": "quarterly"},
                correlation_id="invoke-1",
                idempotency_key="durable-key",
            )

            self.assertEqual(run_id, "durable-run")
            self.assertEqual(calls[0], ("consumer-report", dict))
            self.assertEqual(calls[1][0], {"topic": "quarterly"})
            self.assertEqual(calls[1][1].key, "durable-key")
            self.assertEqual(
                calls[1][1].additional_metadata,
                {
                    "harnest.correlation_id": "invoke-1",
                    "harnest.plugin": "hatchet",
                },
            )
            self.assertFalse(transport._client.stubs.last_workflow.wait_for_result)

    async def test_recovery_transport_uses_application_service_credential(self):
        """Keep startup recovery independent from invocation credential context."""

        invocation = _InvocationContinuations()
        provider = _ProviderContinuations(invocation)
        async with self._active_plugin(provider) as (module, _plugin):
            client_module = __import__(
                f"{module.__name__}.lib.client", fromlist=["HatchetSDKTransport"]
            )
            transport = object()
            constructor = Mock(return_value=transport)
            with (
                patch.dict(
                    os.environ,
                    {"HATCHET_CLIENT_TOKEN": "application-service-token"},
                ),
                patch.object(
                    client_module, "HatchetSDKTransport", constructor
                ),
            ):
                opened = await client_module.open_hatchet_recovery_transport()

            self.assertIs(opened, transport)
            self.assertEqual(constructor.call_count, 1)
            # Check only invocation shape so credential material never appears
            # in assertion output when this boundary regresses.
            self.assertEqual(len(constructor.call_args.args), 1)

    def _active_plugin(self, provider):
        """Copy and activate the real example through Harnest namespace loading."""

        return _ActivePluginFixture(provider)


class _ActivePluginFixture:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.temporary = None
        self.descriptors = ()
        self.plugin = None
        self.token = None

    async def __aenter__(self):
        """Activate and bind the real public singleton for one isolated test."""

        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "plugins"
        shutil.copytree(_SOURCE, root / "hatchet")
        self.descriptors = discover_runtime_plugins(root)
        activated = activate_runtime_plugins(self.descriptors)
        module = activated[0].module
        self.plugin = module.plugin
        await self.plugin.start(
            SimpleNamespace(
                continuations=self.provider,
                root_agent_name="consumer",
            )
        )
        view = self.plugin.create_context(PluginContext("hatchet"))
        PluginContext._bind_continuations(view, self.provider.invocation)
        self.token = Plugin._bind_context(self.plugin, view)
        return module, self.plugin

    async def __aexit__(self, exc_type, exc, traceback):
        """Release context, plugin resources, and controlled namespace in order."""

        del exc_type, exc, traceback
        if self.plugin is not None and self.token is not None:
            Plugin._reset_context(self.plugin, self.token)
            await self.plugin.stop()
        if self.descriptors:
            release_runtime_plugins(self.descriptors)
        if self.temporary is not None:
            self.temporary.cleanup()


def _agent_context():
    """Create the correlation identity required by the plugin's run operation."""

    return create_agent_context(
        framework="langgraph",
        agent_name="consumer",
        invocation_id="invoke-1",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
    )


def _pending(continuation_id: str, external_id: str, run_id: str):
    """Build only the private recovery fields exposed by the provider port."""

    return SimpleNamespace(
        external_id=external_id,
        record=SimpleNamespace(
            continuation_id=continuation_id,
            run_id=run_id,
        ),
    )
