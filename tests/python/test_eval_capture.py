import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from harnest import lifecycle
from harnest._eval_capture_runtime import (
    EvalCaptureRuntimeDriver,
    eval_capture_policy,
)
from harnest.eval_capture import (
    CaptureRecord,
    LocalCaptureStore,
    ToolActivity,
    default_redactor,
    redact_text,
)
from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.lifecycle import LifecycleListener, eval_capture_options
from harnest.runtime_contract import InvocationRequest, InvocationResult


STORAGE = textwrap.dedent(
    """
    from harnest import lifecycle
    from harnest.eval_capture import LocalCaptureStore
    from harnest.store import MemoryStore

    @lifecycle.storage.sessions
    @lifecycle.storage.checkpoints
    def state_store():
        return MemoryStore()

    @lifecycle.storage.custom("captures")
    def captures():
        return LocalCaptureStore({directory!r})
    """
)
CAPTURE = textwrap.dedent(
    """
    from harnest import lifecycle

    @lifecycle.evals.capture(store="captures")
    def should_capture(event):
        return event.tool_error
    """
)


class MemoryStore:
    """In-memory CaptureStore for runtime tests."""

    def __init__(self):
        self.records = []

    async def start(self):
        return None

    async def close(self):
        return None

    async def append(self, record):
        self.records.append(record)

    async def list(self, *, since=None, limit=100):
        return list(reversed(self.records))[:limit]


class FakeDriver:
    """Runtime driver returning fixed events, or raising when told to."""

    info = type("Info", (), {"name": "refunds"})()

    def __init__(self, events=(), text="", error=None):
        self.events = list(events)
        self.text = text
        self.error = error
        self.closed = False
        self.started = False

    async def start(self):
        self.started = True

    async def invoke(self, request):
        if self.error is not None:
            raise self.error
        return InvocationResult(self.text, self.events, None, request.session_id, {})

    async def stream(self, request):
        for event in self.events:
            yield event

    async def close(self):
        self.closed = True


def request(text="Refund order 42 for ana@example.com"):
    return InvocationRequest(
        input=text,
        user_id="user-7",
        session_id="session-1",
        invocation_id="inv-1",
        metadata={"tier": "enterprise"},
        state_delta={},
        transport="response",
    )


TOOL_ERROR_EVENTS = (
    {"type": "tool_call", "id": "c1", "name": "refund", "arguments": {"order": 42}},
    {"type": "tool_result", "id": "c1", "name": "refund", "result": {"error": "declined"}},
    {"type": "message", "text": "Sorry, call +44 20 7946 0958."},
)


def listener(phase, callback, name=None):
    return LifecycleListener(
        phase=phase,
        callback=callback,
        order=0,
        relative_path="root/lifecycle/evals_improvement.py",
        line=1,
        function_name=callback.__name__,
        registration_name=name,
    )


def policy_for(rule, store, *, sample_rate=None, redact=None, environ=None):
    decorated = lifecycle.evals.capture(store="captures", sample_rate=sample_rate)(rule)
    listeners = [listener("eval_capture", decorated, "captures")]
    if redact is not None:
        listeners.append(listener("eval_redact", lifecycle.evals.redact(redact)))
    return eval_capture_policy(listeners, {"captures": store}, environ or {})


async def run_invoke(driver):
    await driver.invoke(request())
    await driver.close()


class DecoratorTests(unittest.TestCase):
    def test_capture_records_store_and_rate(self):
        def rule(event):
            return True

        decorated = lifecycle.evals.capture(store="captures", sample_rate=0.25)(rule)

        options = eval_capture_options(decorated)
        self.assertEqual((options.store, options.sample_rate), ("captures", 0.25))

    def test_rejects_invalid_rates_and_store_names(self):
        for rate in (-0.1, 1.5, True, "0.1"):
            with self.subTest(rate=rate), self.assertRaises((TypeError, ValueError)):
                lifecycle.evals.capture(store="captures", sample_rate=rate)
        with self.assertRaises(ValueError):
            lifecycle.evals.capture(store="bad name")


class DiscoveryTests(unittest.TestCase):
    def discover(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "lifecycle"
            root.mkdir()
            for name, source in files.items():
                (root / name).write_text(
                    source.format(directory=str(Path(directory) / "captures")),
                    encoding="utf-8",
                )
            return discover_extensions(root, framework="adk")

    def test_capture_in_reserved_file_is_discovered(self):
        found = self.discover(
            {"storage.py": STORAGE, "evals_improvement.py": CAPTURE}
        )

        phases = [item.phase for item in found.listeners]
        self.assertIn("eval_capture", phases)

    def test_no_file_means_no_capture_and_no_error(self):
        found = self.discover({"storage.py": STORAGE})

        self.assertNotIn("eval_capture", [item.phase for item in found.listeners])

    def test_capture_outside_reserved_file_is_refused(self):
        with self.assertRaisesRegex(ExtensionDiscoveryError, "evals_improvement.py"):
            self.discover({"storage.py": STORAGE, "hooks.py": CAPTURE})

    def test_missing_or_unsuitable_store_is_refused(self):
        no_store = STORAGE.split('@lifecycle.storage.custom("captures")')[0]
        with self.assertRaisesRegex(ExtensionDiscoveryError, "no @lifecycle.storage.custom"):
            self.discover({"storage.py": no_store, "evals_improvement.py": CAPTURE})
        plain = STORAGE.replace(
            "return LocalCaptureStore({directory!r})", "return Plain()"
        ) + textwrap.dedent(
            """
            class Plain:
                async def start(self):
                    return None

                async def close(self):
                    return None
            """
        )
        with self.assertRaisesRegex(ExtensionDiscoveryError, "CaptureStore"):
            self.discover({"storage.py": plain, "evals_improvement.py": CAPTURE})

    def test_rule_must_take_one_argument_and_appear_once(self):
        two_args = CAPTURE.replace("def should_capture(event):", "def should_capture(event, other):")
        with self.assertRaisesRegex(ExtensionDiscoveryError, "exactly one argument"):
            self.discover({"storage.py": STORAGE, "evals_improvement.py": two_args})
        twice = CAPTURE + CAPTURE.replace("should_capture", "again").replace(
            "from harnest import lifecycle", ""
        )
        with self.assertRaisesRegex(ExtensionDiscoveryError, "declared once"):
            self.discover({"storage.py": STORAGE, "evals_improvement.py": twice})


class RuntimeCaptureTests(unittest.TestCase):
    def test_rule_sees_content_but_store_gets_only_structure(self):
        """Selection can inspect private facts without copying them into storage."""
        store = MemoryStore()
        seen = []

        def rule(event):
            """Retain the event to verify the rule's view independently of storage."""
            seen.append(event)
            return event.tool_error

        policy = policy_for(rule, store)
        driver = EvalCaptureRuntimeDriver(FakeDriver(TOOL_ERROR_EVENTS, "Sorry"), policy)

        with patch("harnest._eval_capture_runtime._AUDIT") as audit:
            asyncio.run(run_invoke(driver))

        self.assertEqual(seen[0].input_text, request().input)
        self.assertEqual(seen[0].output_text, "Sorry")
        self.assertEqual(seen[0].user_id, "user-7")
        self.assertEqual(seen[0].session_id, "session-1")
        self.assertEqual(seen[0].metadata, {"tier": "enterprise"})
        self.assertEqual(seen[0].tool_calls[0].arguments, {"order": 42})
        self.assertEqual(seen[0].tool_calls[0].result, {"error": "declined"})
        (record,) = store.records
        self.assertEqual(record.reasons, ("rule",))
        self.assertEqual(record.content, "omitted")
        self.assertEqual((record.input_text, record.output_text), ("", ""))
        self.assertEqual(record.tool_calls, (ToolActivity("refund", error=True),))
        self.assertEqual(record.to_json(), {
            "schemaVersion": 1, "id": record.id, "capturedAt": record.captured_at,
            "agentName": "refunds", "invocationId": "inv-1", "reasons": ["rule"],
            "status": "completed", "errorType": None, "inputText": "", "outputText": "",
            "toolCalls": [{"name": "refund", "arguments": None, "result": None, "error": True}],
            "content": "omitted",
        })
        audit.info.assert_called_once_with(
            "eval.capture.stored", trigger="runtime", outcome="stored",
            agent="refunds", reason="rule",
        )

    def test_response_is_unchanged_when_rule_or_store_fails(self):
        def broken(event):
            raise RuntimeError("bug in rule")

        store = MemoryStore()
        inner = FakeDriver(TOOL_ERROR_EVENTS, "Sorry")
        driver = EvalCaptureRuntimeDriver(inner, policy_for(broken, store))

        async def scenario():
            result = await driver.invoke(request())
            await driver.close()
            return result

        result = asyncio.run(scenario())

        self.assertEqual(result.text, "Sorry")
        self.assertEqual(store.records, [])
        self.assertTrue(inner.closed)

    def test_failed_invocation_is_captured_and_still_raises(self):
        """Failure diagnostics survive omission while the caller sees the same error."""
        store = MemoryStore()
        driver = EvalCaptureRuntimeDriver(
            FakeDriver(error=ValueError("model down")), policy_for(lambda e: e.failed, store)
        )

        async def scenario():
            """Drain capture work after the original invocation error reaches the caller."""
            with self.assertRaises(ValueError):
                await driver.invoke(request())
            await driver.close()

        asyncio.run(scenario())

        self.assertEqual(store.records[0].error_type, "ValueError")
        self.assertEqual(store.records[0].status, "failed")
        self.assertEqual(store.records[0].content, "omitted")
        self.assertEqual(store.records[0].input_text, "")

    def test_stream_text_is_joined_and_custom_redactor_runs(self):
        """Sync and async policies receive complete content and own the retained values."""
        store = MemoryStore()
        seen = []

        def redact(record):
            """Apply a deterministic team policy without setting the runtime marker."""
            seen.append(record)
            return replace(
                record, input_text="approved input", output_text="approved output",
                tool_calls=(ToolActivity("refund", {"approved": True}, "reviewed", True),),
            )

        async def redact_async(record):
            """Exercise awaiting a team policy before its result is stored."""
            await asyncio.sleep(0)
            return redact(record)

        async def scenario():
            """Consume streamed responses unchanged and drain background capture."""
            events = []
            async for _ in driver.stream(request()):
                events.append(_)
            await driver.close()
            return events

        for redactor in (redact, redact_async):
            with self.subTest(redactor=redactor.__name__):
                policy = policy_for(lambda e: True, store, redact=redactor)
                driver = EvalCaptureRuntimeDriver(FakeDriver(TOOL_ERROR_EVENTS), policy)
                self.assertEqual(asyncio.run(scenario()), list(TOOL_ERROR_EVENTS))
                self.assertEqual(seen[-1].input_text, request().input)
                self.assertEqual(seen[-1].output_text, "Sorry, call +44 20 7946 0958.")
                self.assertEqual(seen[-1].tool_calls[0].arguments, {"order": 42})
                record = store.records[-1]
                self.assertEqual(record.content, "redacted")
                self.assertEqual(record.input_text, "approved input")
                self.assertEqual(record.output_text, "approved output")
                self.assertEqual(record.tool_calls[0].result, "reviewed")
                self.assertEqual(CaptureRecord.from_json(record.to_json()), record)

    def test_redactor_failure_drops_capture_without_logging_content(self):
        """A policy failure cannot leak its payload through responses or audit logs."""
        store = MemoryStore()

        def redact(record):
            """Include private content in an error to exercise the logging boundary."""
            raise ValueError(record.input_text)

        driver = EvalCaptureRuntimeDriver(
            FakeDriver(text="original response"), policy_for(lambda e: True, store, redact=redact)
        )

        async def scenario():
            """Observe the response before waiting for a failing background redactor."""
            result = await driver.invoke(request())
            await driver.close()
            return result

        with patch("harnest._eval_capture_runtime._AUDIT") as audit:
            result = asyncio.run(scenario())
        self.assertEqual(result.text, "original response")
        self.assertEqual(store.records, [])
        audit.warning.assert_called_once_with(
            "eval.capture.failed", trigger="runtime", outcome="failed",
            agent="refunds", error="ValueError",
        )
        audit.info.assert_not_called()

    def test_redactor_must_return_a_record(self):
        """Invalid sync and async redactor returns are dropped, not serialized."""
        store = MemoryStore()

        async def invalid_async(record):
            """Return an invalid payload after the async policy boundary."""
            return {"raw": record}

        for redactor in (lambda record: {"raw": record}, invalid_async):
            with self.subTest(redactor=redactor):
                policy = policy_for(lambda e: True, store, redact=redactor)
                with patch("harnest._eval_capture_runtime._AUDIT") as audit:
                    asyncio.run(run_invoke(EvalCaptureRuntimeDriver(FakeDriver(), policy)))
                self.assertEqual(store.records, [])
                audit.warning.assert_called_once_with(
                    "eval.capture.failed", trigger="runtime", outcome="failed",
                    agent="refunds", error="TypeError",
                )
                audit.info.assert_not_called()

    def test_code_rate_wins_over_environment_and_switch_turns_off(self):
        store = MemoryStore()
        environ = {"HARNEST_EVAL_CAPTURE_SAMPLE_RATE": "0.5"}

        authored = policy_for(lambda e: False, store, sample_rate=0.1, environ=environ)
        from_env = policy_for(lambda e: False, store, environ=environ)
        switched = policy_for(
            lambda e: True, store, environ={"HARNEST_EVAL_CAPTURE": "off"}
        )

        self.assertEqual(authored.sample_rate, 0.1)
        self.assertEqual(from_env.sample_rate, 0.5)
        self.assertIsNone(switched)

    def test_sampling_captures_rejected_invocations(self):
        """Sampling follows the same structure-only default as rule acceptance."""
        store = MemoryStore()
        policy = policy_for(lambda e: False, store, sample_rate=1.0)

        asyncio.run(run_invoke(EvalCaptureRuntimeDriver(FakeDriver(), policy)))

        self.assertEqual(store.records[0].reasons, ("sample",))
        self.assertEqual(store.records[0].content, "omitted")
        self.assertEqual(store.records[0].input_text, "")

    def test_wrapper_delegates_start_and_other_attributes(self):
        inner = FakeDriver()
        inner.external_continuations = "continuations"
        driver = EvalCaptureRuntimeDriver(inner, policy_for(lambda e: True, MemoryStore()))

        asyncio.run(driver.start())

        self.assertTrue(inner.started)
        self.assertEqual(driver.external_continuations, "continuations")
        self.assertEqual(driver.info.name, "refunds")

    def test_absent_capture_listener_means_no_policy(self):
        self.assertIsNone(eval_capture_policy([], {"captures": MemoryStore()}, {}))


class LocalCaptureStoreTests(unittest.TestCase):
    def record(self, captured_at, record_id="r1"):
        """Create an explicitly team-redacted record for persistence checks."""
        return CaptureRecord(
            id=record_id,
            captured_at=captured_at.isoformat(),
            agent_name="refunds",
            invocation_id="inv",
            reasons=("rule",),
            status="completed",
            input_text="hi",
            output_text="hello",
            tool_calls=(ToolActivity("refund", {"order": 1}, {"ok": True}),),
            content="redacted",
        )

    def test_omitted_content_is_removed_from_disk_and_readback(self):
        """The JSON boundary strips payloads even when callers bypass runtime capture."""
        record = replace(self.record(datetime.now(timezone.utc)), content="omitted")
        with tempfile.TemporaryDirectory() as directory:
            store = LocalCaptureStore(directory)

            async def scenario():
                """Persist and reload using the actual JSON Lines store."""
                await store.start()
                await store.append(record)
                return await store.list()

            (loaded,) = asyncio.run(scenario())
            serialized = json.loads(next(Path(directory).glob("*.jsonl")).read_text())
        self.assertEqual(loaded, default_redactor(record))
        self.assertEqual(serialized, loaded.to_json())
        self.assertEqual(serialized["content"], "omitted")
        self.assertEqual(serialized["inputText"], "")
        self.assertEqual(serialized["outputText"], "")
        self.assertIsNone(serialized["toolCalls"][0]["arguments"])
        self.assertIsNone(serialized["toolCalls"][0]["result"])
        self.assertNotIn("redacted", serialized)

    def test_missing_or_invalid_content_marker_cannot_retain_payloads(self):
        """Legacy heuristic markers never imply that a team redactor ran."""
        raw = self.record(datetime.now(timezone.utc)).to_json()
        raw.pop("content")
        raw["redacted"] = True
        loaded = CaptureRecord.from_json(raw)
        self.assertEqual(loaded.content, "omitted")
        self.assertEqual((loaded.input_text, loaded.output_text), ("", ""))
        self.assertEqual(loaded.tool_calls, (ToolActivity("refund"),))
        with self.assertRaisesRegex(ValueError, "content"):
            CaptureRecord.from_json(dict(raw, content="unknown"))

    def test_compatibility_helpers_omit_instead_of_guessing_personal_data(self):
        """Existing imports remain available without performing pattern detection."""
        record = self.record(datetime.now(timezone.utc))
        omitted = default_redactor(record)
        self.assertEqual(omitted.content, "omitted")
        self.assertEqual((omitted.input_text, omitted.output_text), ("", ""))
        self.assertEqual(omitted.tool_calls, (ToolActivity("refund"),))
        self.assertEqual(redact_text("ordinary prose, a name, or ana@example.com"), "")

    def test_round_trip_newest_first_and_since_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalCaptureStore(directory)
            now = datetime.now(timezone.utc)

            async def scenario():
                await store.start()
                await store.append(self.record(now - timedelta(minutes=5), "old"))
                await store.append(self.record(now, "new"))
                everything = await store.list()
                recent = await store.list(since=now - timedelta(minutes=1))
                return everything, recent

            everything, recent = asyncio.run(scenario())

        self.assertEqual([item.id for item in everything], ["new", "old"])
        self.assertEqual([item.id for item in recent], ["new"])
        self.assertEqual(everything[0].tool_calls[0].name, "refund")

    def test_retention_removes_old_day_files(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "2000-01-01.jsonl"
            stale.write_text("{}\n", encoding="utf-8")

            asyncio.run(LocalCaptureStore(directory, retention_days=30).start())

            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
