import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from harnest.agent import Agent
from harnest.model import LiteLLMLifecycle, LiteLLMModel
from harnest.model_lifecycle import (
    _LifecycleLiteLLMClient,
    _attach_lifecycle_resource,
    close_litellm_lifecycles,
)


class _Delegate:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"answer": "raw"}

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"answer": "raw"}


class _AsyncLifecycle(LiteLLMLifecycle):
    def __init__(self, name="transport"):
        self.events = []
        self.transport = types.SimpleNamespace(name=name, mtls=True)

    async def create_transport(self, context):
        self.events.append(("create", context.framework, context.transport))
        await asyncio.sleep(0)
        return self.transport

    async def before_request(self, request, context):
        self.events.append(("before", context.transport))
        request["extra_headers"] = {"X-Team": "platform"}
        return request

    async def after_response(self, response, context):
        self.events.append(("after", context.transport))
        return {"wrapped": response}

    async def on_error(self, error, context):
        self.events.append(("error", str(error), context.transport))

    async def close(self, context):
        self.events.append(("close", context.transport))


class _SyncLifecycle(LiteLLMLifecycle):
    def __init__(self):
        self.events = []

    def before_request(self, request, context):
        self.events.append("before")
        request["temperature"] = 0

    def after_response(self, response, context):
        self.events.append("after")
        return response

    def close(self, context):
        self.events.append("close")


class _StreamDelegate(_Delegate):
    def __init__(self, *, stream_error=None):
        super().__init__()
        self.stream_error = stream_error

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)

        async def generate():
            yield {"delta": "one"}
            if self.stream_error is not None:
                raise self.stream_error
            yield {"delta": "two"}

        return generate()

    def completion(self, **kwargs):
        self.calls.append(kwargs)

        def generate():
            yield {"delta": "one"}
            if self.stream_error is not None:
                raise self.stream_error
            yield {"delta": "two"}

        return generate()


class LiteLLMLifecycleTests(unittest.TestCase):
    def test_async_lifecycle_orders_hooks_injects_transport_and_closes(self):
        async def exercise():
            lifecycle = _AsyncLifecycle()
            delegate = _Delegate()
            client = _LifecycleLiteLLMClient(
                delegate,
                lifecycle,
                model="openai/gpt-test",
                framework="adk",
            )
            first, second = await asyncio.gather(
                client.acompletion(model="openai/gpt-test", messages=[]),
                client.acompletion(model="openai/gpt-test", messages=[]),
            )
            await client.aclose()
            await client.aclose()
            return lifecycle, delegate, first, second

        lifecycle, delegate, first, second = asyncio.run(exercise())
        self.assertEqual(first, {"wrapped": {"answer": "raw"}})
        self.assertEqual(second, first)
        self.assertEqual(lifecycle.events[0], ("create", "adk", None))
        self.assertEqual([event[0] for event in lifecycle.events].count("create"), 1)
        self.assertEqual([event[0] for event in lifecycle.events].count("before"), 2)
        self.assertEqual([event[0] for event in lifecycle.events].count("after"), 2)
        self.assertEqual([event[0] for event in lifecycle.events].count("close"), 1)
        for call in delegate.calls:
            self.assertIs(call["client"], lifecycle.transport)
            self.assertEqual(call["extra_headers"], {"X-Team": "platform"})
        self.assertTrue(
            all(
                event[1] is lifecycle.transport
                for event in lifecycle.events
                if event[0] in {"before", "after", "close"}
            )
        )

    def test_error_hook_observes_delegate_failure(self):
        async def exercise():
            lifecycle = _AsyncLifecycle()
            client = _LifecycleLiteLLMClient(
                _Delegate(error=ValueError("gateway failed")),
                lifecycle,
                model="openai/gpt-test",
                framework="langgraph",
            )
            with self.assertRaisesRegex(ValueError, "gateway failed"):
                await client.acompletion(messages=[])
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual([event[0] for event in lifecycle.events], ["create", "before", "error"])

    def test_async_stream_finishes_lifecycle_only_after_exhaustion(self):
        async def exercise():
            lifecycle = _AsyncLifecycle()
            client = _LifecycleLiteLLMClient(
                _StreamDelegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="langgraph",
            )
            stream = await client.acompletion(messages=[], stream=True)
            self.assertNotIn("after", [event[0] for event in lifecycle.events])
            chunks = [chunk async for chunk in stream]
            self.assertEqual([event[0] for event in lifecycle.events][-1], "after")
            await client.aclose()
            return chunks, lifecycle

        chunks, lifecycle = asyncio.run(exercise())
        self.assertEqual(chunks, [{"delta": "one"}, {"delta": "two"}])
        self.assertEqual(
            [event[0] for event in lifecycle.events],
            ["create", "before", "after", "close"],
        )

    def test_async_stream_iteration_failure_notifies_error_without_after(self):
        async def exercise():
            lifecycle = _AsyncLifecycle()
            client = _LifecycleLiteLLMClient(
                _StreamDelegate(stream_error=ValueError("stream failed")),
                lifecycle,
                model="openai/gpt-test",
                framework="adk",
            )
            stream = await client.acompletion(messages=[], stream=True)
            with self.assertRaisesRegex(ValueError, "stream failed"):
                _ = [chunk async for chunk in stream]
            await client.aclose()
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual(
            [event[0] for event in lifecycle.events],
            ["create", "before", "error", "close"],
        )

    def test_async_stream_cancellation_is_observed_and_propagated(self):
        class CancellationDelegate(_Delegate):
            async def acompletion(self, **kwargs):
                async def generate():
                    await asyncio.Event().wait()
                    yield None

                return generate()

        async def exercise():
            lifecycle = _AsyncLifecycle()
            client = _LifecycleLiteLLMClient(
                CancellationDelegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="langgraph",
            )
            stream = await client.acompletion(messages=[], stream=True)
            reading = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            reading.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await reading
            await client.aclose()
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual(
            [event[0] for event in lifecycle.events],
            ["create", "before", "error", "close"],
        )

    def test_sync_stream_success_and_failure_have_terminal_hooks(self):
        success_lifecycle = _SyncLifecycle()
        success = _LifecycleLiteLLMClient(
            _StreamDelegate(),
            success_lifecycle,
            model="openai/gpt-test",
            framework="langgraph",
        )
        self.assertEqual(
            list(success.completion(messages=[], stream=True)),
            [{"delta": "one"}, {"delta": "two"}],
        )
        self.assertEqual(success_lifecycle.events, ["before", "after"])

        failure_lifecycle = _SyncLifecycle()
        failure = _LifecycleLiteLLMClient(
            _StreamDelegate(stream_error=ValueError("stream failed")),
            failure_lifecycle,
            model="openai/gpt-test",
            framework="langgraph",
        )
        with self.assertRaisesRegex(ValueError, "stream failed"):
            list(failure.completion(messages=[], stream=True))
        self.assertEqual(failure_lifecycle.events, ["before"])

    def test_sync_hooks_are_supported_without_hidden_event_loop(self):
        lifecycle = _SyncLifecycle()
        delegate = _Delegate()
        client = _LifecycleLiteLLMClient(
            delegate,
            lifecycle,
            model="openai/gpt-test",
            framework="langgraph",
        )

        self.assertEqual(client.completion(messages=[]), {"answer": "raw"})
        client.close()

        self.assertEqual(delegate.calls[0]["temperature"], 0)
        self.assertEqual(lifecycle.events, ["before", "after", "close"])

    def test_async_hook_rejects_sync_execution_and_mixed_modes(self):
        async_client = _LifecycleLiteLLMClient(
            _Delegate(),
            _AsyncLifecycle(),
            model="openai/gpt-test",
            framework="langgraph",
        )
        with self.assertRaisesRegex(RuntimeError, "create_transport is async"):
            async_client.completion(messages=[])

        sync_client = _LifecycleLiteLLMClient(
            _Delegate(),
            _SyncLifecycle(),
            model="openai/gpt-test",
            framework="langgraph",
        )
        sync_client.completion(messages=[])
        with self.assertRaisesRegex(RuntimeError, "cannot mix"):
            asyncio.run(sync_client.acompletion(messages=[]))

    def test_async_close_can_retry_after_sync_rejection(self):
        class AsyncCloseLifecycle(_SyncLifecycle):
            async def close(self, context):
                self.events.append("close")

        lifecycle = AsyncCloseLifecycle()
        client = _LifecycleLiteLLMClient(
            _Delegate(),
            lifecycle,
            model="openai/gpt-test",
            framework="langgraph",
        )
        with self.assertRaisesRegex(RuntimeError, "close is async"):
            client.close()
        asyncio.run(client.aclose())
        asyncio.run(client.aclose())
        self.assertEqual(lifecycle.events, ["close"])

    def test_failed_async_close_is_retryable_and_success_is_idempotent(self):
        class RetryLifecycle(_SyncLifecycle):
            def __init__(self):
                super().__init__()
                self.close_calls = 0

            async def close(self, context):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise ValueError("cleanup failed")

        async def exercise():
            lifecycle = RetryLifecycle()
            client = _LifecycleLiteLLMClient(
                _Delegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="adk",
            )
            with self.assertRaisesRegex(ValueError, "cleanup failed"):
                await client.aclose()
            await client.aclose()
            await client.aclose()
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual(lifecycle.close_calls, 2)

    def test_async_close_waits_for_transport_initialization_and_call(self):
        class BlockingLifecycle(_AsyncLifecycle):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def create_transport(self, context):
                self.started.set()
                await self.release.wait()
                return await super().create_transport(context)

        async def exercise():
            lifecycle = BlockingLifecycle()
            client = _LifecycleLiteLLMClient(
                _Delegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="adk",
            )
            call = asyncio.create_task(client.acompletion(messages=[]))
            await lifecycle.started.wait()
            closing = asyncio.create_task(client.aclose())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            lifecycle.release.set()
            await call
            await closing
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual([event[0] for event in lifecycle.events][-1], "close")

    def test_failed_transport_initialization_can_retry_without_leaking_state(self):
        class RetryTransportLifecycle(_AsyncLifecycle):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            async def create_transport(self, context):
                self.attempts += 1
                if self.attempts == 1:
                    raise ValueError("transport unavailable")
                return await super().create_transport(context)

        async def exercise():
            lifecycle = RetryTransportLifecycle()
            client = _LifecycleLiteLLMClient(
                _Delegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="langgraph",
            )
            with self.assertRaisesRegex(ValueError, "transport unavailable"):
                await client.acompletion(messages=[])
            result = await client.acompletion(messages=[])
            await client.aclose()
            return lifecycle, result

        lifecycle, result = asyncio.run(exercise())
        self.assertEqual(result, {"wrapped": {"answer": "raw"}})
        self.assertEqual(lifecycle.attempts, 2)

    def test_close_waits_until_returned_stream_is_released(self):
        async def exercise():
            lifecycle = _AsyncLifecycle()
            client = _LifecycleLiteLLMClient(
                _StreamDelegate(),
                lifecycle,
                model="openai/gpt-test",
                framework="langgraph",
            )
            stream = await client.acompletion(messages=[], stream=True)
            closing = asyncio.create_task(client.aclose())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            await stream.aclose()
            await closing
            return lifecycle

        lifecycle = asyncio.run(exercise())
        self.assertEqual([event[0] for event in lifecycle.events][-1], "close")

    def test_transport_and_cleanup_are_isolated_per_built_adk_model(self):
        class FakeADKClient(_Delegate):
            pass

        class FakeLiteLlm:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        lite_llm = types.ModuleType("google.adk.models.lite_llm")
        lite_llm.LiteLlm = FakeLiteLlm
        lite_llm.LiteLLMClient = FakeADKClient
        modules = {"google.adk.models.lite_llm": lite_llm}
        first_lifecycle = _AsyncLifecycle("first")
        second_lifecycle = _AsyncLifecycle("second")

        with patch.dict(sys.modules, modules):
            first = LiteLLMModel(
                "openai/gpt-test", lifecycle=first_lifecycle
            ).build()
            second = LiteLLMModel(
                "openai/gpt-test", lifecycle=second_lifecycle
            ).build()

        async def exercise():
            await first.llm_client.acompletion(
                model="openai/gpt-test", messages=[], tools=[]
            )
            await second.llm_client.acompletion(
                model="openai/gpt-test", messages=[], tools=[]
            )
            await close_litellm_lifecycles(first)

        asyncio.run(exercise())
        self.assertIs(
            first.llm_client.__harnest_controller__._delegate.calls[0]["client"],
            first_lifecycle.transport,
        )
        self.assertIs(
            second.llm_client.__harnest_controller__._delegate.calls[0]["client"],
            second_lifecycle.transport,
        )
        self.assertEqual([event[0] for event in first_lifecycle.events][-1], "close")
        self.assertNotIn("close", [event[0] for event in second_lifecycle.events])

    def test_lifecycle_rejects_ambiguous_client_configuration(self):
        with self.assertRaisesRegex(ValueError, "cannot be configured together"):
            LiteLLMModel(
                "openai/gpt-test",
                lifecycle=_SyncLifecycle(),
                client=object(),
            )

    def test_request_hook_cannot_replace_owned_transport(self):
        class ReplacingLifecycle(_SyncLifecycle):
            def create_transport(self, context):
                return object()

            def before_request(self, request, context):
                request["client"] = object()
                return request

        client = _LifecycleLiteLLMClient(
            _Delegate(),
            ReplacingLifecycle(),
            model="openai/gpt-test",
            framework="langgraph",
        )
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            client.completion(messages=[])

    def test_agent_owns_model_lifecycle_until_runtime_cleanup(self):
        class FakeADKClient(_Delegate):
            pass

        class Recording:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        agents = types.ModuleType("google.adk.agents")
        agents.LlmAgent = Recording
        lite_llm = types.ModuleType("google.adk.models.lite_llm")
        lite_llm.LiteLlm = Recording
        lite_llm.LiteLLMClient = FakeADKClient
        lifecycle = _AsyncLifecycle()
        modules = {
            "google.adk.agents": agents,
            "google.adk.models.lite_llm": lite_llm,
        }

        with patch.dict(sys.modules, modules):
            built = Agent(
                name="support",
                model=LiteLLMModel("openai/gpt-test", lifecycle=lifecycle),
                instruction="Help.",
            ).build()

        asyncio.run(close_litellm_lifecycles(built))
        self.assertEqual([event[0] for event in lifecycle.events], ["close"])

    def test_cleanup_continues_after_failure_and_surfaces_primary_error(self):
        calls = []

        class Resource:
            def __init__(self, name, error=None):
                self.name = name
                self.error = error

            async def aclose(self):
                calls.append(self.name)
                if self.error is not None:
                    raise self.error

        owner = types.SimpleNamespace()
        _attach_lifecycle_resource(owner, Resource("first", ValueError("first")))
        _attach_lifecycle_resource(owner, Resource("second", RuntimeError("second")))
        with self.assertRaisesRegex(RuntimeError, "second") as caught:
            asyncio.run(close_litellm_lifecycles(owner))
        self.assertEqual(calls, ["second", "first"])
        notes = getattr(caught.exception, "__notes__", ())
        if notes:
            self.assertIn("ValueError", notes[0])
            self.assertNotIn("first", notes[0])


if __name__ == "__main__":
    unittest.main()
