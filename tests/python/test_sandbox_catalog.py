"""Verify named sandbox assignment is opt-in across recursive agent composition."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from harnest import Agent, BundleConventionError, BundleExportError, bundle_agent
from harnest.bundle import _discover_sandboxes, _resolve_graph_resources
from harnest.graph import Edge, Graph, START
from harnest.sandbox_catalog import sandbox_catalog_scope


class SandboxCatalogTests(unittest.TestCase):
    def setUp(self):
        """Use real source discovery with factories that must remain uncalled."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write("agent.py", "# anchor\n")
        self.write("instructions.md", "Use only assigned sandboxes.\n")
        for name in ("calculations", "research", "restricted"):
            self.sandbox(name)

    def write(self, relative, source):
        """Write only test-owned authored resources."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def sandbox(self, name, prefix=""):
        """Declare a portable provider without permitting eager construction."""
        self.write(
            f"{prefix}sandbox/{name}.py",
            "from harnest import Sandbox\n"
            "def build():\n    raise AssertionError('provider constructed during compilation')\n"
            f"{name} = Sandbox.provider(build, name={name!r}, metadata={{'profile': {name!r}}})\n",
        )

    def compose(self, names=(), **kwargs):
        """Exercise the public composition boundary rather than resolver internals."""
        with patch.object(Agent, "build", lambda definition: definition):
            return bundle_agent(
                self.root / "agent.py", Agent(name="root", model="unused", sandboxes=names, **kwargs),
            )

    def test_registry_presence_does_not_grant_any_sandbox(self):
        definition = self.compose()
        self.assertEqual(dict(definition._sandbox_bindings), {})
        self.assertFalse(hasattr(definition, "sandbox"))

    def test_multiple_grants_resolve_exact_names_in_authored_order(self):
        definition = self.compose(["research", "calculations"])
        self.assertEqual(tuple(definition._sandbox_bindings), ("research", "calculations"))
        self.assertEqual(definition._sandbox_bindings["research"].metadata["profile"], "research")
        self.assertNotIn("restricted", definition._sandbox_bindings)

    def test_unknown_grant_explains_available_names_and_fails_closed(self):
        with self.assertRaisesRegex(BundleConventionError, "unknown sandboxes: missing") as caught:
            self.compose(["calculations", "missing"])
        self.assertIn("Available names: calculations, research, restricted", str(caught.exception))

    def test_flat_children_require_their_own_assignments(self):
        self.write("subagents/worker.py", "from harnest import Agent\nworker = Agent(name='worker', model='unused', instruction='Work.', sandboxes=['research'])\n")
        self.write("subagents/observer.py", "from harnest import Agent\nobserver = Agent(name='observer', model='unused', instruction='Observe.')\n")
        definition = self.compose(["calculations"])
        children = {child.name: child for child in definition.subagents}
        self.assertEqual(tuple(children["worker"]._sandbox_bindings), ("research",))
        self.assertEqual(dict(children["observer"]._sandbox_bindings), {})

    def test_nested_child_can_assign_root_and_local_definitions(self):
        self.write("subagents/worker/agent.py", "from harnest import Agent\nworker = Agent(name='worker', model='unused', sandboxes=['research', 'private_work'])\n")
        self.write("subagents/worker/instructions.md", "Work.\n")
        self.sandbox("private_work", "subagents/worker/")
        definition = self.compose()
        self.assertEqual(tuple(definition.subagents[0]._sandbox_bindings), ("research", "private_work"))
        self.assertEqual(dict(definition._sandbox_bindings), {})
        with self.assertRaisesRegex(BundleConventionError, "unknown sandboxes: private_work"):
            self.compose(["private_work"])

    def test_nested_child_cannot_shadow_ancestor_configuration(self):
        self.write("subagents/worker/agent.py", "from harnest import Agent\nworker = Agent(name='worker', model='unused')\n")
        self.write("subagents/worker/instructions.md", "Work.\n")
        self.sandbox("research", "subagents/worker/")
        with self.assertRaisesRegex(BundleConventionError, "duplicate an ancestor declaration"):
            self.compose()

    def test_inline_children_and_grandchildren_get_only_their_own_grants(self):
        grandchild = Agent(name="grandchild", model="unused", instruction="Work.", sandboxes=["research"])
        child = Agent(name="child", model="unused", instruction="Work.", subagents=[grandchild])
        definition = self.compose(["calculations"], subagents=[child])
        child = definition.subagents[0]
        self.assertEqual(dict(child._sandbox_bindings), {})
        self.assertEqual(tuple(child.subagents[0]._sandbox_bindings), ("research",))

    def test_failed_compilation_cannot_leak_catalog_to_next_project(self):
        with self.assertRaises(BundleConventionError):
            self.compose(["missing"])
        other = self.root / "other"
        other.mkdir()
        (other / "agent.py").write_text("# anchor\n")
        (other / "instructions.md").write_text("Work.\n")
        with self.assertRaisesRegex(BundleConventionError, "Available names: none"):
            bundle_agent(other / "agent.py", Agent(name="other", model="unused", sandboxes=["research"]))

    def test_named_export_and_single_declaration_per_file_are_required(self):
        self.write("sandbox/wrong.py", "from harnest import Sandbox\nother = Sandbox.provider(lambda: None)\n")
        with self.assertRaisesRegex(BundleExportError, "must export 'wrong'"):
            _discover_sandboxes(self.root / "sandbox")
        self.write("sandbox/wrong.py", "from harnest import Sandbox\nwrong = Sandbox.provider(lambda: None)\nextra = Sandbox.provider(lambda: None)\n")
        with self.assertRaisesRegex(BundleExportError, "additional sandbox resources"):
            _discover_sandboxes(self.root / "sandbox")

    def test_public_compile_of_nested_project_does_not_inherit_catalog(self):
        other = self.root / "other"
        other.mkdir()
        (other / "agent.py").write_text("# anchor\n")
        (other / "instructions.md").write_text("Work.\n")
        with sandbox_catalog_scope(self.root, _discover_sandboxes):
            with self.assertRaisesRegex(BundleConventionError, "Available names: none"):
                bundle_agent(other / "agent.py", Agent(name="other", model="unused", sandboxes=["research"]))

    def test_repeated_folder_scope_reuses_exact_provider_declarations(self):
        """Graph references may revisit a folder without recreating its factories."""
        from harnest.sandbox_catalog import current_sandbox_catalog

        nested = self.root / "subagents" / "worker"
        self.sandbox("private_work", "subagents/worker/")
        with sandbox_catalog_scope(self.root, _discover_sandboxes):
            with sandbox_catalog_scope(nested, _discover_sandboxes):
                first = current_sandbox_catalog()["private_work"]
            with sandbox_catalog_scope(nested, _discover_sandboxes):
                self.assertIs(current_sandbox_catalog()["private_work"], first)

    def test_callable_graph_can_leave_sandbox_catalog_unused(self):
        graph = Graph(name="root", nodes={"work": lambda value: value}, edges=[Edge(START, "work")])
        resolved = _resolve_graph_resources(self.root / "agent.py", graph, framework="adk")
        self.assertEqual(set(resolved.nodes), {"work"})

    def test_graph_agents_receive_only_explicit_assignments(self):
        graph = Graph(
            name="root",
            nodes={
                "worker": Agent(name="worker", model="unused", sandboxes=["research"]),
                "observer": Agent(name="observer", model="unused"),
            },
            edges=[Edge(START, "worker"), Edge(START, "observer")],
        )
        for framework in ("adk", "langgraph"):
            resolved = _resolve_graph_resources(self.root / "agent.py", graph, framework=framework)
            self.assertEqual(tuple(resolved.nodes["worker"]._sandbox_bindings), ("research",))
            self.assertEqual(dict(resolved.nodes["observer"]._sandbox_bindings), {})
