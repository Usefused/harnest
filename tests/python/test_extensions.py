import tempfile
import unittest
from pathlib import Path

from harnest.extension_loader import ExtensionDiscoveryError, discover_extensions
from harnest.output import OutputPolicy
from harnest.session import InMemorySessionStore
from harnest.telemetry import TelemetryExporterError, resolve_telemetry_exporters


class ExtensionDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _session_store(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "sessions.py").write_text(
            "from harnest.lifecycle import lifecycle\n"
            "from harnest.session import InMemorySessionStore\n"
            "@lifecycle.session_store\n"
            "def session_store(): return InMemorySessionStore()\n",
            encoding="utf-8",
        )
        (root / "checkpoints.py").write_text(
            "from harnest.checkpoint import MemoryStore\n"
            "from harnest.lifecycle import lifecycle\n"
            "@lifecycle.checkpointer\n"
            "def checkpointer(): return MemoryStore()\n",
            encoding="utf-8",
        )

    def test_discovers_arbitrary_nested_files_and_orders_shared_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            (root / "nested").mkdir(parents=True)
            self._session_store(root)
            (root / "zeta.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "def helper(): return 'ignored'\n"
                "@lifecycle.before_invoke(order=10)\n"
                "def late(context, value): return value\n",
                encoding="utf-8",
            )
            (root / "nested" / "alpha.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.before_invoke(order=10)\n"
                "def first(context, value): return value\n"
                "@lifecycle.before_invoke(order=-1)\n"
                "def earliest(context, value): return value\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="adk")

        self.assertEqual(
            [item.function_name for item in result.listeners],
            ["earliest", "first", "late"],
        )
        self.assertEqual(result.native, ())
        self.assertIsInstance(result.session_store, InMemorySessionStore)

    def test_discovers_the_required_session_store_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            self._session_store(root)
            result = discover_extensions(root, framework="adk")
        self.assertIsInstance(result.session_store, InMemorySessionStore)
        self.assertEqual(result.listeners, ())

    def test_discovers_the_required_checkpoint_authority(self):
        from harnest.checkpoint import MemoryStore

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)

            result = discover_extensions(root, framework="langgraph")

        self.assertIsInstance(result.checkpointer, MemoryStore)

    def test_telemetry_exporter_factories_are_repeatable_and_runtime_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "telemetry.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.telemetry_exporter\n"
                "def first(): raise RuntimeError('runtime only')\n"
                "@lifecycle.telemetry_exporter(order=10)\n"
                "def second(): return object()\n",
                encoding="utf-8",
            )

            discovered = discover_extensions(root, framework="langgraph")

        self.assertEqual(
            [item.function_name for item in discovered.telemetry_exporters],
            ["first", "second"],
        )
        self.assertNotIn(
            "telemetry_exporter", [item.phase for item in discovered.listeners]
        )
        with self.assertRaisesRegex(
            TelemetryExporterError, "first.*RuntimeError"
        ):
            resolve_telemetry_exporters(discovered.telemetry_exporters)

    def test_telemetry_exporter_factory_requires_zero_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "telemetry.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.telemetry_exporter\n"
                "def exporter(endpoint): return endpoint\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "must accept no arguments"
            ):
                discover_extensions(root, framework="adk")

    def test_output_policy_defaults_to_suppress_and_can_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            default = discover_extensions(root, framework="langgraph")
            (root / "output.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.output import OutputPolicy\n"
                "@lifecycle.output_policy\n"
                "def output_policy():\n"
                "    return OutputPolicy(subagent_messages='include')\n",
                encoding="utf-8",
            )
            configured = discover_extensions(root, framework="langgraph")

        self.assertEqual(default.output_policy, OutputPolicy())
        self.assertEqual(configured.output_policy.subagent_messages, "include")
        self.assertNotIn(
            "output_policy", [item.phase for item in configured.listeners]
        )

    def test_output_policy_factory_is_unique_and_typed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "output.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.output_policy\n"
                "def output_policy(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "OutputPolicy"):
                discover_extensions(root, framework="adk")
            (root / "other_output.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.output import OutputPolicy\n"
                "@lifecycle.output_policy\n"
                "def other_output(): return OutputPolicy()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "at most one"):
                discover_extensions(root, framework="adk")

    def test_requires_exactly_one_typed_checkpointer_factory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "checkpoints.py").unlink()
            with self.assertRaisesRegex(ExtensionDiscoveryError, "found 0"):
                discover_extensions(root, framework="adk")
            (root / "checkpoints.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "checkpoint authority"):
                discover_extensions(root, framework="adk")

    def test_harnest_checkpointer_must_implement_the_store_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "checkpoints.py").write_text(
                "from harnest.checkpoint import HarnestStore\n"
                "from harnest.lifecycle import lifecycle\n"
                "class IncompleteStore(HarnestStore): pass\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return IncompleteStore()\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "does not implement CheckpointStore"
            ):
                discover_extensions(root, framework="adk")

    def test_native_adk_store_is_the_single_session_and_checkpoint_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)
            (root / "storage.py").write_text(
                "from harnest.checkpoint import ADKStore\n"
                "from harnest.lifecycle import lifecycle\n"
                "store = ADKStore(object())\n"
                "@lifecycle.session_store\n"
                "def session_store(): return store\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return store\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="adk")

        self.assertIs(result.session_store, result.checkpointer)

    def test_native_adk_store_rejects_a_competing_session_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._session_store(root)
            (root / "checkpoints.py").write_text(
                "from harnest.checkpoint import ADKStore\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.checkpointer\n"
                "def checkpointer(): return ADKStore(object())\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "same ADKStore object"
            ):
                discover_extensions(root, framework="adk")

    def test_undecorated_public_helpers_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "helpers.py").write_text("def parse(value): return value\n")
            result = discover_extensions(root, framework="langgraph")
        self.assertEqual(result.listeners, ())

    def test_runtime_resource_factory_is_discovered_without_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            self._session_store(root)
            (root / "retrieval.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.resource\n"
                "def vector_client():\n"
                "  raise RuntimeError('compile must not create clients')\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="langgraph")

        self.assertEqual(
            [item.function_name for item in result.listeners], ["vector_client"]
        )

    def test_context_providers_are_discovered_without_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            self._session_store(root)
            (root / "memory.py").write_text(
                "from harnest.context import context\n"
                "@context('request_cache')\n"
                "async def request_cache():\n"
                "  raise RuntimeError('compile must not create context values')\n",
                encoding="utf-8",
            )
            (root / "client.py").write_text(
                "from harnest.context import context\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.resource\n"
                "@context('memory')\n"
                "async def memory():\n"
                "  yield object()\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="langgraph")

        self.assertEqual(
            [(item.phase, item.context_name) for item in result.listeners],
            [("resource", "memory"), ("context", "request_cache")],
        )

    def test_storage_is_exposed_only_when_its_factory_declares_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            (root / "storage.py").write_text(
                "from harnest.checkpoint import MemoryStore\n"
                "from harnest.context import context\n"
                "from harnest.lifecycle import lifecycle\n"
                "store = MemoryStore()\n"
                "@lifecycle.session_store\n"
                "@context('sessions')\n"
                "def session_store(): return store\n"
                "@lifecycle.checkpointer\n"
                "@context('checkpoints')\n"
                "def checkpointer(): return store\n",
                encoding="utf-8",
            )

            result = discover_extensions(root, framework="adk")

        self.assertEqual(
            [item.name for item in result.context_values],
            ["sessions", "checkpoints"],
        )
        self.assertIs(result.context_values[0].value, result.session_store)
        self.assertIs(result.context_values[1].value, result.checkpointer)

    def test_context_names_and_lifecycle_roles_are_strict(self):
        for source, message in (
            (
                "from harnest.context import context\n"
                "@context('same')\ndef first(): return 1\n"
                "@context('same')\ndef second(): return 2\n",
                "duplicate context resource names",
            ),
            (
                "from harnest.context import context\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.before_invoke\n"
                "@context('invalid')\n"
                "def before(ctx, value): return value\n",
                "cannot also use @lifecycle.before_invoke",
            ),
            (
                "from harnest.context import context\n"
                "@context('invalid')\n"
                "def invalid():\n  yield object()\n",
                "combine it with @lifecycle.resource",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "extensions"
                self._session_store(root)
                (root / "providers.py").write_text(source, encoding="utf-8")

                with self.assertRaisesRegex(ExtensionDiscoveryError, message):
                    discover_extensions(root, framework="adk")

    def test_runtime_resource_factory_signature_is_validated_without_calling_it(self):
        for source, message in (
            (
                "@lifecycle.resource\ndef resource(value): return value\n",
                "no arguments",
            ),
            (
                "@lifecycle.resource\nasync def resource(): return None\n",
                "must be synchronous",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "extensions"
                self._session_store(root)
                (root / "resource.py").write_text(
                    "from harnest.lifecycle import lifecycle\n" + source,
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ExtensionDiscoveryError, message):
                    discover_extensions(root, framework="adk")

    def test_adk_plugin_factory_is_explicit_and_validated(self):
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: F401
        except ImportError:
            self.skipTest("google-adk is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "audit.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.adk_plugin(order=4)\n"
                "def plugin():\n"
                "  from google.adk.plugins.base_plugin import BasePlugin\n"
                "  return BasePlugin(name='audit')\n",
                encoding="utf-8",
            )
            result = discover_extensions(root, framework="adk")
        self.assertEqual([item.name for item in result.native], ["audit"])

    def test_wrong_native_value_and_factory_arguments_are_rejected(self):
        for source, message in (
            (
                "@lifecycle.adk_plugin\ndef plugin(value): return value\n",
                "no arguments",
            ),
            ("@lifecycle.adk_plugin\ndef plugin(): return object()\n", "BasePlugin"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "extensions"
                root.mkdir(parents=True)
                self._session_store(root)
                (root / "bad.py").write_text(
                    "from harnest.lifecycle import lifecycle\n" + source,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ExtensionDiscoveryError, message):
                    discover_extensions(root, framework="adk")

    def test_public_non_python_resources_and_invalid_framework_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "notes.txt").write_text("not executable")
            with self.assertRaisesRegex(ExtensionDiscoveryError, "Python files"):
                discover_extensions(root, framework="adk")
        with self.assertRaisesRegex(ExtensionDiscoveryError, "unsupported"):
            discover_extensions("missing", framework="other")

    def test_opposite_framework_native_factory_fails_instead_of_being_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "native.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.langgraph_middleware\n"
                "def middleware(): return object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "targets langgraph"):
                discover_extensions(root, framework="adk")

    def test_portable_listener_signature_is_validated_at_compile_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "invalid.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.on_event\n"
                "def event(context): return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "exactly two"):
                discover_extensions(root, framework="adk")

    def test_decorated_function_alias_is_rejected_as_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            root.mkdir(parents=True)
            self._session_store(root)
            (root / "duplicate.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.on_error\n"
                "def notify(context, error): pass\n"
                "also_notify = notify\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "multiple names"):
                discover_extensions(root, framework="adk")

    def test_requires_exactly_one_session_store_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "extensions"
            with self.assertRaisesRegex(
                ExtensionDiscoveryError, "exactly one.*found 0"
            ):
                discover_extensions(root, framework="adk")

            self._session_store(root)
            (root / "other.py").write_text(
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.session import InMemorySessionStore\n"
                "@lifecycle.session_store\n"
                "def other(): return InMemorySessionStore()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtensionDiscoveryError, "exactly one.*found 2"):
                discover_extensions(root, framework="adk")

    def test_session_store_factory_is_synchronous_zero_argument_and_typed(self):
        cases = (
            (
                "@lifecycle.session_store\ndef session_store(value): return value\n",
                "no arguments",
            ),
            (
                "@lifecycle.session_store\nasync def session_store(): return None\n",
                "synchronous",
            ),
            (
                "@lifecycle.session_store\ndef session_store(): return object()\n",
                "must return SessionStore",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "extensions"
                root.mkdir(parents=True)
                (root / "sessions.py").write_text(
                    "from harnest.lifecycle import lifecycle\n" + source,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ExtensionDiscoveryError, message):
                    discover_extensions(root, framework="langgraph")


if __name__ == "__main__":
    unittest.main()
