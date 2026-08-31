import unittest
from dataclasses import FrozenInstanceError, replace

from harnest.application import CompiledApplication, RuntimeCapabilities
from harnest.assets import MemoryAssetStore
from harnest.checkpoint import MemoryStore
from harnest.context import ContextValue
from harnest.credentials import CredentialProvider, CredentialRequest
from harnest.http_routes import AgentInvoker, HTTPRouteExtension
from harnest.lifecycle import LifecycleListener
from harnest.output import OutputPolicy
from harnest.skills import SkillRegistry, SkillScope


class _CredentialProvider(CredentialProvider):
    """Provide a valid inert capability for application-boundary tests."""

    async def resolve(self, request: CredentialRequest):
        """Return no credential because these tests never enter an invocation."""

        return None


def _listener(phase: str = "telemetry_exporter") -> LifecycleListener:
    """Build one deterministic lifecycle factory descriptor."""

    return LifecycleListener(phase, lambda: None, 0, "telemetry.py", 1, "exporter")


class RuntimeCapabilitiesTests(unittest.TestCase):
    def test_application_normalizes_capabilities_and_preserves_legacy_access(self):
        """Keep existing attributes as exact aliases of the grouped values."""

        store = MemoryStore()
        asset_store = MemoryAssetStore()
        credential_provider = _CredentialProvider()
        route = HTTPRouteExtension(object(), AgentInvoker(), "routes.py:1:routes")
        policy = OutputPolicy(subagent_messages="include")
        telemetry = _listener()
        context_value = ContextValue("sessions", store, "sessions.py:1:sessions")
        skill_registry = SkillRegistry({"root": SkillScope()})
        application = CompiledApplication(
            name="root",
            framework="langgraph",
            mode="managed",
            target=object(),
            session_store=store,
            checkpointer=store,
            asset_store=asset_store,
            credential_provider=credential_provider,
            http_routes=[route],
            output_policy=policy,
            telemetry_exporters=[telemetry],
            context_values=[context_value],
            skill_registry=skill_registry,
        )

        capabilities = application.runtime_capabilities
        for name in (
            "session_store",
            "checkpointer",
            "asset_store",
            "storage_registry",
            "credential_provider",
            "http_routes",
            "output_policy",
            "telemetry_exporters",
            "context_values",
            "skill_registry",
        ):
            self.assertIs(getattr(application, name), getattr(capabilities, name))
        self.assertEqual(application.http_routes, (route,))
        self.assertEqual(application.telemetry_exporters, (telemetry,))
        self.assertEqual(application.context_values, (context_value,))
        self.assertIs(capabilities.storage_registry.sessions, store)
        self.assertIs(capabilities.storage_registry.checkpoints, store)
        self.assertEqual(application.lifecycle_coverage.mode, "managed")
        self.assertEqual(application.lifecycle_coverage.report()["tool"], "full")
        with self.assertRaises(FrozenInstanceError):
            capabilities.output_policy = OutputPolicy()  # type: ignore[misc]

    def test_dataclass_replace_rebuilds_capability_aliases(self):
        """Preserve the replacement pattern used by runtime application fixtures."""

        original = CompiledApplication(
            name="root", framework="adk", mode="managed", target=object()
        )
        policy = OutputPolicy(subagent_messages="include")

        replaced = replace(original, output_policy=policy)

        self.assertIs(replaced.output_policy, policy)
        self.assertIs(replaced.runtime_capabilities.output_policy, policy)
        self.assertIsNot(replaced.runtime_capabilities, original.runtime_capabilities)

    def test_advanced_application_reports_only_harnest_owned_coverage(self):
        """Make native-framework escape hatches visible to deployment tooling."""

        application = CompiledApplication(
            name="root", framework="adk", mode="advanced", target=object()
        )

        report = application.lifecycle_coverage.report()
        self.assertEqual(report["tool"], "wrapped-only")
        self.assertEqual(report["checkpoint"], "framework-owned")

    def test_capability_boundary_rejects_wrong_resource_types(self):
        """Fail invalid resources once before any transport consumes them."""

        invalid_values = (
            ("session_store", object(), "SessionStore"),
            ("checkpointer", object(), "CheckpointAuthority"),
            ("asset_store", object(), "AssetStore"),
            ("custom_stores", {"users": object()}, "CustomStorage"),
            ("credential_provider", object(), "CredentialProvider"),
            ("http_routes", (object(),), "HTTPRouteExtension"),
            ("output_policy", object(), "OutputPolicy"),
            ("telemetry_exporters", (object(),), "LifecycleListener"),
            ("context_values", (object(),), "ContextValue"),
            ("skill_registry", object(), "SkillRegistry"),
        )
        for field_name, value, message in invalid_values:
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(TypeError, message):
                    RuntimeCapabilities(**{field_name: value})

    def test_capability_boundary_rejects_ambiguous_or_wrong_phase_entries(self):
        """Reject collections whose elements are typed but semantically ambiguous."""

        duplicate = ContextValue("store", object(), "first.py:1:store")
        with self.assertRaisesRegex(ValueError, "duplicate context resource names"):
            RuntimeCapabilities(context_values=(duplicate, duplicate))
        with self.assertRaisesRegex(TypeError, "telemetry_exporter factories"):
            RuntimeCapabilities(telemetry_exporters=(_listener("before_invoke"),))


if __name__ == "__main__":
    unittest.main()
