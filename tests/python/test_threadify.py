"""Optional Threadify business filtering and lifecycle integration contracts."""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from _session_store_fixture import write_session_store

from harnest import lifecycle
from harnest.bundle import compile_application
from harnest.http_lifecycle import HTTPLifecycleMiddleware
from harnest.lifecycle_runtime import LifecycleRuntimeDriver
from harnest.runtime_contract import AgentInfo, InvocationRequest, InvocationResult, SessionRecord
from harnest_threadify import BusinessFilter, Threadify
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode


def listener(phase, callback):
    """Build the same runtime callback identity produced by source discovery."""
    return lifecycle.LifecycleListener(phase, callback, 0, "integration.py", 1, phase)


class NativeThread:
    """Record public SDK step calls without importing or contacting Threadify."""

    def __init__(self, thread_id):
        """Keep a stable identity and observable exported business steps."""
        self.thread_id = thread_id
        self.steps = []

    def step(self, name):
        """Return an SDK-shaped builder with async terminal methods."""
        step = NativeStep(name)
        self.steps.append(step)
        return step


class NativeStep:
    """Mirror only the public Threadify step builder used by the adapter."""

    def __init__(self, name):
        """Store exported fields for payload and idempotency assertions."""
        self.name = name
        self.data = {}
        self.success = AsyncMock()
        self.failed = AsyncMock()

    def idempotency_key(self, key):
        """Preserve deterministic export identity for retries."""
        self.key = key
        return self

    def add_context(self, data):
        """Record exactly what would cross the remote service boundary."""
        self.data = data
        return self


class SDK:
    """Emulate remote durable lookup across multiple local client instances."""

    def __init__(self):
        """Track creations separately from lookups and SDK shutdown."""
        self.refs = {}
        self.threads = {}
        self.start_count = 0
        self.close = AsyncMock()
        self.get_thread_by_ref = AsyncMock(side_effect=self.lookup)
        self.start = AsyncMock(side_effect=self.create)
        self.join = AsyncMock(side_effect=self.resume)

    async def lookup(self, key, value):
        """Match the native SDK's archived thread identity response."""
        return self.refs.get(value)

    async def create(self, *, label, refs):
        """Commit one remote thread and its owner-scoped reference."""
        self.start_count += 1
        thread = NativeThread(str(self.start_count))
        self.threads[thread.thread_id] = thread
        self.refs[refs['harnestSession']] = SimpleNamespace(id=thread.thread_id)
        return thread

    async def resume(self, *, thread_id, role):
        """Return the existing thread without closing or replacing it."""
        return self.threads[thread_id]


class Driver:
    """Minimal backend to test the common ADK/LangGraph lifecycle boundary."""

    def __init__(self, framework, tracer):
        """Use a real tracer while keeping agent inference offline."""
        self.info = AgentInfo('support', 'support', '', {}, framework=framework)
        self.tracer = tracer
        self.fail_create = False

    async def create_session(self, *, session_id, user_id, state):
        """Commit a session unless the test injects a backend failure."""
        if self.fail_create:
            raise RuntimeError('database unavailable')
        return SessionRecord(session_id, user_id, state)

    async def invoke(self, request):
        """Emit nested framework noise and a business decision under real context."""
        with self.tracer.start_as_current_span('invoke_llm'):
            with self.tracer.start_as_current_span('harnest.decision.evaluate') as span:
                span.set_attribute('harnest.decision.action', 'route')
                span.set_attribute('gen_ai.prompt', 'private customer text')
        return InvocationResult('answer', (), None, request.session_id, {})

    async def close(self):
        """Provide the runtime shutdown contract without external resources."""


class ThreadifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        """Use isolated providers so global OTel state cannot contaminate tests."""
        self.sdk = SDK()
        self.provider = TracerProvider(resource=Resource({'secret': 'resource secret'}))
        self.capture = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.capture))
        self.client = Threadify(service_name='support', filter=BusinessFilter(attributes=('order.id',)))
        self.factory = SimpleNamespace(connect=AsyncMock(return_value=self.sdk))
        self.patches = [
            patch.object(Threadify, 'native_class', self.factory),
            patch('harnest_threadify.client.trace.get_tracer_provider', return_value=self.provider),
            patch('harnest_threadify.client.get_tracer', side_effect=self.provider.get_tracer),
            patch.dict(os.environ, {'THREADIFY_API_KEY': 'private token'}),
        ]
        for item in self.patches:
            item.start()
        await self.client.__aenter__()

    async def asyncTearDown(self):
        """Close SDK resources before stopping the isolated telemetry provider."""
        await self.client.__aexit__(None, None, None)
        self.provider.shutdown()
        for item in reversed(self.patches):
            item.stop()

    async def test_session_reuse_concurrency_owner_isolation_and_recovery(self):
        """One local session stays stable; equal IDs across owners never collide."""
        threads = await asyncio.gather(*[
            self.client.session(user_id='alice', session_id='same') for _ in range(8)
        ])
        self.assertEqual({t.thread_id for t in threads}, {'1'})
        other = await self.client.session(user_id='bob', session_id='same')
        self.assertEqual(other.thread_id, '2')
        self.client._bindings.clear()
        recovered = await self.client.session(user_id='alice', session_id='same')
        self.assertEqual(recovered.thread_id, '1')
        self.assertEqual(self.sdk.start_count, 2)
        refs = self.sdk.start.call_args_list[0].kwargs['refs']
        self.assertEqual(refs['sessionId'], 'same')
        self.assertNotIn('alice', str(refs))

    async def test_ambiguous_creation_is_not_retried(self):
        """An uncertain remote mutation cannot produce a second local start."""
        self.sdk.start.side_effect = TimeoutError('secret provider response')
        for _ in range(2):
            self.assertIsNone(await self.client.session(user_id='u', session_id='s'))
        self.assertEqual(self.sdk.start.await_count, 1)

    async def test_lookup_failure_does_not_start_replacement(self):
        """Lookup unavailability must not be interpreted as a missing thread."""
        self.sdk.get_thread_by_ref.side_effect = RuntimeError('private detail')
        self.assertIsNone(await self.client.session(user_id='u', session_id='s'))
        self.sdk.start.assert_not_awaited()

    async def test_both_frameworks_link_decisions_and_filter_noise(self):
        """The common driver supplies ownership before nested framework work."""
        for framework in ('adk', 'langgraph'):
            driver = LifecycleRuntimeDriver(Driver(framework, self.provider.get_tracer(framework)), [
                listener('before_invoke', self.client.before_invocation),
                listener('session_created', self.client.session_created),
            ])
            await driver.create_session(session_id=framework, user_id='u', state={'secret': 'state'})
            request = InvocationRequest('private input', 'u', framework, 'i', {}, {})
            await driver.invoke(request)
            await driver.close()
        spans = self.capture.get_finished_spans()
        clean = [s for value in spans if (s := self.client.filter.sanitize(value, service_name='support'))]
        self.assertEqual([s.name for s in clean], ['harnest.decision.evaluate'] * 2)
        self.assertNotEqual(clean[0].attributes['threadify.thread_id'], clean[1].attributes['threadify.thread_id'])
        self.assertNotIn('private', str([dict(s.attributes) for s in clean]))
        await self.client.export_spans(clean)
        self.assertEqual(self.sdk.threads['1'].steps[0].data['harnest.decision.action'], 'route')

    async def test_filter_scrubs_events_status_resources_and_nested_values(self):
        """Allowlisting creates a fresh span without mutating other exporters' data."""
        await self.client.session(user_id='u', session_id='s')
        tracer = self.provider.get_tracer('agent')
        with tracer.start_as_current_span('business.payment') as span:
            self.client.annotate(span, 'u', 's')
            span.set_attributes({'order.id': 'order-1', 'tool.arguments': 'private', 'gen_ai.prompt': 'private'})
            span.add_event('exception', {'exception.message': 'private'})
            span.set_status(Status(StatusCode.ERROR, 'private error'))
        original = self.capture.get_finished_spans()[-1]
        clean = self.client.filter.sanitize(original, service_name='support')
        self.assertEqual(clean.attributes['order.id'], 'order-1')
        self.assertFalse(clean.events)
        self.assertIsNone(clean.status.description)
        self.assertEqual(dict(clean.resource.attributes), {'service.name': 'support'})
        self.assertNotIn('tool.arguments', clean.attributes)
        self.assertTrue(original.events)
        self.assertEqual(original.attributes['tool.arguments'], 'private')
        self.assertNotIn('order.id', self.client.filter._attributes({'order.id': {'nested': 'private'}}))

    async def test_native_export_waits_for_delivery_without_closing_session(self):
        """Synchronous batching runs off-loop and returns after accepted SDK steps."""
        await self.client.session(user_id='u', session_id='s')
        with self.provider.get_tracer('agent').start_as_current_span('business.done') as span:
            self.client.annotate(span, 'u', 's')
        exporter = self.client.telemetry_exporter().traces
        result = await asyncio.to_thread(exporter.export, self.capture.get_finished_spans())
        self.assertIs(result, SpanExportResult.SUCCESS)
        step = self.sdk.threads['1'].steps[0]
        step.success.assert_awaited_once()
        self.assertEqual(len(step.key), 49)
        self.sdk.close.assert_not_awaited()

    async def test_request_scope_covers_streaming_and_resets_after_failure(self):
        """Cancellation unwinds the request span without exporting exception payloads."""
        async def app(scope, receive, send):
            """Link a successful creation before sending two response chunks."""
            await self.client.session_created(None, SessionRecord('s', 'u', {}))
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'private', 'more_body': True})
            raise asyncio.CancelledError('private disconnect')
        async def send(message):
            """Observe that the business span remains open during body emission."""
            self.assertFalse(self.capture.get_finished_spans())
        middleware = HTTPLifecycleMiddleware(app, listeners=[listener('http_scope', self.client.request_scope)])
        with self.assertRaises(asyncio.CancelledError):
            await middleware({'type': 'http', 'method': 'POST', 'path': '/responses'}, None, send)
        span = self.capture.get_finished_spans()[-1]
        self.assertIs(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes['business.outcome'], 'failed')
        self.assertFalse(span.events)
        self.assertEqual(span.attributes['threadify.ref.sessionId'], 's')

    async def test_otlp_protobuf_contains_only_filtered_business_data(self):
        """Inspect the real OTLP encoder output before an HTTP request is sent."""
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from harnest_threadify.exporter import BusinessExporter
        await self.client.session(user_id='u', session_id='s')
        with self.provider.get_tracer('private-scope').start_as_current_span('business.refund') as span:
            self.client.annotate(span, 'u', 's')
            span.set_attribute('order.id', 'O-42')
            span.set_attribute('gen_ai.prompt', 'PAYLOAD_SECRET')
            span.add_event('exception', {'exception.message': 'ERROR_SECRET'})
        transport = OTLPSpanExporter(endpoint='http://unused.invalid/v1/traces')
        response = SimpleNamespace(ok=True, status_code=200)
        with patch.object(transport._session, 'post', return_value=response) as post:
            result = BusinessExporter(self.client, transport).export(self.capture.get_finished_spans())
        self.assertIs(result, SpanExportResult.SUCCESS)
        payload = ExportTraceServiceRequest.FromString(post.call_args.kwargs['data'])
        self.assertIn('O-42', str(payload))
        self.assertNotIn('SECRET', str(payload))
        self.assertNotIn('private-scope', str(payload))
        self.assertNotIn('resource secret', str(payload))
        transport.shutdown()

    async def test_business_tools_are_explicitly_selected(self):
        """Built-in skill and model utility calls do not become business steps."""
        self.client.business_tools = frozenset({'issue_refund'})
        await self.client.session(user_id='u', session_id='s')
        active = SimpleNamespace(user_id='u', session_id='s')
        with patch('harnest_threadify.exporter.optional_active_context', return_value=active):
            self.client._tool_outcome('list_skills', 'completed')
            self.client._tool_outcome('issue_refund', 'failed')
        spans = self.capture.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].attributes['business.tool'], 'issue_refund')
        self.assertIs(spans[0].status.status_code, StatusCode.ERROR)

    async def test_session_observers_only_follow_commits(self):
        """An observer cannot make committed persistence look like a failed mutation."""
        backend = Driver('adk', self.provider.get_tracer('test'))
        callback = AsyncMock(side_effect=RuntimeError('observer failed'))
        driver = LifecycleRuntimeDriver(backend, [listener('session_created', callback)])
        record = await driver.create_session(session_id='s', user_id='u', state={})
        self.assertEqual(record.id, 's')
        backend.fail_create = True
        with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            await driver.create_session(session_id='s2', user_id='u', state={})
        self.assertEqual(callback.await_count, 1)
        await driver.close()


class OptionalImportTests(unittest.TestCase):
    def test_configuration_is_deferred_and_filters_require_exact_attribute_names(self):
        """Constructing config must not connect or require an API key."""
        with patch.dict(os.environ, {}, clear=True):
            client = Threadify(service_name='agent')
            self.assertIsNone(client.loop)
        with self.assertRaises(ValueError):
            BusinessFilter(attributes=('business.*',))


class ThreadifyCompilerTests(unittest.TestCase):
    def test_optional_config_compiles_for_adk_and_langgraph_without_credentials(self):
        """Exercise discovery of the real opt-in example without starting its SDK."""
        source = Path(__file__).resolve().parents[2] / "packages/harnest-threadify/examples/telemetry.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_session_store(root)
            (root / 'agent.py').write_text(
                "from harnest.agent import Agent\nfrom harnest.model import LiteLLMModel\n"
                "root_agent = Agent(name='support', model=LiteLLMModel('openai/test', api_key='unused'))\n"
            )
            (root / 'instructions.md').write_text('Help with support.')
            shutil.copyfile(source, root / 'lifecycle/telemetry.py')
            for framework in ('adk', 'langgraph'):
                with patch.dict(os.environ, {}, clear=True):
                    app = compile_application(root, entrypoint="agent:root_agent", framework=framework)
                phases = {item.phase for item in app.lifecycle_extensions}
                self.assertTrue({'resource', 'session_created', 'before_invoke', 'http_scope'}.issubset(phases))
                self.assertEqual(len(app.telemetry_exporters), 1)
