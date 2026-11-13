"""Backed-up framework migration across authored modules and extension manifests."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml

from harnest.extension_descriptors import ExtensionConventionError, discover_extensions
from harnest.upgrade import PROJECT_SCHEMA, UpgradeError, apply_upgrade, plan_upgrade
from harnest.upgrade_frameworks import _manifest_source


def _write(root, path, source):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    return target


def _project(root, framework="adk"):
    """Create a schema-six project without importing any authored module."""
    _write(root, "config.yaml", f'''apiVersion: harnest.dev/v1alpha1
kind: Agent
metadata:
  name: upgrade-example
spec:
  entrypoint: agent:root_agent
  framework:
    name: {framework}
    mode: managed
  runtime:
    version: "3.12"
    dependencyFile: pyproject.toml
''')
    _write(root, "pyproject.toml", '[project]\nname="upgrade-example"\nversion="0.0.0"\nrequires-python=">=3.11"\ndependencies=[]\n')
    _write(root, "harnest.lock", 'apiVersion: harnest.dev/v1alpha1\nkind: ProjectLock\nprojectSchema: 6\n')
    _write(root, "agent.py", 'from harnest.agent import Agent\nroot_agent = Agent(name="example", model="test/model")\n')
    _write(root, "instructions.md", 'Reply briefly.\n')
    _write(root, "agent-card.yaml", 'name: Upgrade example\ndescription: Upgrade fixture.\n')
    _write(root, "lifecycle/storage.py", '''from harnest import lifecycle
from harnest.store import MemoryStore
store = MemoryStore()
@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state(): return store
''')


def _extension(root, *, legacy=False):
    """Keep the package executable marker inert during migration planning."""
    folder = "plugins" if legacy else "extensions"
    filename = "plugin" if legacy else "extension"
    kind = "RuntimePlugin" if legacy else "Extension"
    _write(root, f"{folder}/audit/{filename}.py", 'raise AssertionError("must not import")\n')
    return _write(root, f"{folder}/audit/{filename}.yaml", f'''apiVersion: harnest.dev/v1alpha1
kind: {kind}
metadata:
  name: audit
  version: 1.0.0
runtime:
  entrypoint: {filename}:{filename}
capabilities:
  - 'native.adk' # keep capability explanation
''')


class FrameworkUpgradeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "langgraph is not installed")
    def test_generated_advanced_graph_upgrades_imported_store_and_runs(self):
        """Upgrade shared library exports, then compile and execute the graph offline."""
        from harnest.bundle import compile_artifact
        from harnest.runtime import run_agent_message

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            _project(root, "langgraph")
            # Advanced graphs do not need a separate prompt file to upgrade or run.
            (root / "instructions.md").unlink()
            config = root / "config.yaml"
            config.write_text(config.read_text().replace("mode: managed", "mode: advanced"))
            _write(root, "lib/storage.py", 'from harnest.store import MemoryStore\nstore = MemoryStore()\n')
            _write(root, "models/default.py", 'from harnest.model import LiteLLMModel\nmodel = LiteLLMModel("openai/test")\n')
            _write(root, "lib/models.py", 'from harnest.models.default import model\n')
            model_user = _write(root, "lib/chat.py", 'from .models import model\ndef chat(): return model.build_for("langgraph")\n')
            _write(root, "lifecycle/storage.py", '''from harnest import lifecycle
from harnest.lib.storage import store
@lifecycle.storage.sessions
@lifecycle.storage.checkpoints
def state(): return store
''')
            agent = _write(root, "agent.py", '''from langgraph.graph import END, START, StateGraph
from harnest.agent import Agent
from harnest.lib.storage import store

def reply(state): return {"answer": "upgraded:" + state["prompt"]}
builder = StateGraph(dict)
builder.add_node("reply", reply)
builder.add_edge(START, "reply")
builder.add_edge("reply", END)
graph = builder.compile(checkpointer=store.as_langgraph_checkpointer())
root_agent = Agent.advanced(
    graph, name="example",
    input_adapter=lambda text, state: {**state, "prompt": text},
    output_adapter=lambda state: state["answer"],
)
''')
            plan = plan_upgrade(root)
            self.assertEqual(plan.blockers, ())
            apply_upgrade(plan)
            self.assertIn("store.framework_checkpointer()", agent.read_text())
            self.assertIn("model.build()", model_user.read_text())
            self.assertEqual(plan_upgrade(root).actions, ())
            output = Path(directory) / "artifact"
            compile_artifact(root, output, framework="langgraph", mode="advanced")
            self.assertEqual(asyncio.run(run_agent_message(output, "hello"))["text"], "upgraded:hello")

    def test_plan_composes_code_and_manifests_with_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project(root)
            source = '''from harnest.model import LiteLLMModel
from harnest.runtime import create_fastapi_app, load_compiled_app
raise AssertionError("must not import")
model = LiteLLMModel("openai/test").build_for("adk")
app = create_fastapi_app("artifact", adk_session_storage=storage)
# adk_session_storage and build_for in comments remain untouched
'''
            module = _write(root, "lib/host.py", source)
            hook = _write(root, "lifecycle/adk.py", '''from harnest import lifecycle as hooks
@hooks.adk_plugin
def extension(): return plugin
''')
            manifest = _extension(root)
            before = manifest.read_text()
            with self.assertRaises(ExtensionConventionError):
                discover_extensions(root / "extensions")
            plan = plan_upgrade(root)
            self.assertEqual(plan.blockers, ())
            self.assertEqual(module.read_text(), source)
            self.assertEqual(manifest.read_text(), before)
            backup = apply_upgrade(plan)
            self.assertEqual((backup / "lib/host.py").read_text(), source)
            self.assertEqual((backup / "extensions/audit/extension.yaml").read_text(), before)
            self.assertIn('.build()', module.read_text())
            self.assertIn('framework_session_storage=storage', module.read_text())
            self.assertIn('load_framework_app as load_compiled_app', module.read_text())
            self.assertIn('@hooks.framework_extension', hook.read_text())
            self.assertIn("'framework.extension' # keep capability explanation", manifest.read_text())
            self.assertEqual(discover_extensions(root / "extensions")[0].capabilities, ("framework.extension",))
            self.assertEqual(yaml.safe_load((root / "harnest.lock").read_text())["projectSchema"], PROJECT_SCHEMA)
            self.assertEqual(plan_upgrade(root).actions, ())

    def test_legacy_package_manifest_composes_before_directory_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project(root)
            path = _extension(root, legacy=True)
            _write(root, "plugins/audit/plugin.py", "from harnest.runtime_plugin import Plugin\nplugin = Plugin()\n")
            original = path.read_text()
            plan = plan_upgrade(root)
            self.assertEqual(plan.blockers, ())
            backup = apply_upgrade(plan)
            self.assertEqual((backup / "plugins/audit/plugin.yaml").read_text(), original)
            updated = yaml.safe_load((root / "extensions/audit/extension.yaml").read_text())
            self.assertEqual(updated["kind"], "Extension")
            self.assertEqual(updated["capabilities"], ["framework.extension"])
            self.assertEqual(plan_upgrade(root).actions, ())

    def test_shared_definitions_and_new_import_targets_invalidate_a_reviewed_plan(self):
        """Imports influence rewrites even when the defining file needs no change."""
        for change in ("definition", "new_package", "framework"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _project(root, "langgraph")
                definition = _write(root, "lib/storage.py", "from harnest.store import MemoryStore\nstore=MemoryStore()\n")
                _write(root, "lib/use.py", "from .storage import store\ncheckpointer=store.as_langgraph_checkpointer()\n")
                plan = plan_upgrade(root)
                self.assertEqual(plan.blockers, ())
                if change == "definition":
                    definition.write_text("from other import Store\nstore=Store()\n")
                elif change == "new_package":
                    _write(root, "lib/storage/__init__.py", "from other import store\n")
                else:
                    config = root / "config.yaml"
                    config.write_text(config.read_text().replace("name: langgraph", "name: adk"))
                with self.assertRaisesRegex(UpgradeError, "changed after planning"):
                    apply_upgrade(plan)
                self.assertFalse((root / ".harnest").exists())

    def test_colliding_import_targets_block_instead_of_merging_export_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project(root, "langgraph")
            _write(root, "lib/storage.py", "from harnest.store import MemoryStore\nstore=MemoryStore()\n")
            _write(root, "lib/storage/__init__.py", "from other import store\n")
            path = _write(root, "lib/use.py", "from .storage import store\ncheckpointer=store.as_langgraph_checkpointer()\n")
            plan = plan_upgrade(root)
            self.assertTrue(any("ambiguous module paths" in item for item in plan.blockers))
            with self.assertRaises(UpgradeError):
                apply_upgrade(plan)
            self.assertIn("as_langgraph_checkpointer", path.read_text())

    def test_current_schema_still_migrates_later_added_files_and_prunes_generated_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _project(root)
            apply_upgrade(plan_upgrade(root))
            source = 'from harnest.lifecycle import adk_plugin as extension\n'
            file = _write(root, "tests/test_feature.py", source)
            ignored = _write(root, "node_modules/tool/old.py", 'raise SyntaxError("ignored") !\n')
            plan = plan_upgrade(root)
            self.assertEqual(plan.blockers, ())
            apply_upgrade(plan)
            self.assertIn('framework_extension as extension', file.read_text())
            self.assertIn('raise SyntaxError', ignored.read_text())

    def test_symlinks_are_blocked_without_reading_external_source(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            _project(root)
            target = _write(Path(outside), "source.py", 'raise RuntimeError("outside")\n')
            (root / "linked.py").symlink_to(target)
            plan = plan_upgrade(root)
            self.assertTrue(any("linked.py is a symlink" in item for item in plan.blockers))
            with self.assertRaises(UpgradeError):
                apply_upgrade(plan)
            self.assertFalse((root / ".harnest").exists())


class FrameworkManifestTests(unittest.TestCase):
    def test_literal_lists_merge_capabilities_without_changing_comments_or_other_values(self):
        for capabilities in (
            "[native.adk, native.langgraph, native.extension, framework.extension] # preserve",
            "\n  - native.adk # first\n  - 'native.langgraph' # second\n  - framework.extension # third",
            "\n  - native.adk # first\n  -\n    native.langgraph # second",
        ):
            with self.subTest(capabilities=capabilities):
                source = f'kind: Extension\nmetadata: {{name: native.adk}}\ncapabilities: {capabilities}\n'
                updated = _manifest_source(source, "extension.yaml")
                self.assertEqual(yaml.safe_load(updated)["capabilities"], ["framework.extension"])
                self.assertEqual(yaml.safe_load(updated)["metadata"]["name"], "native.adk")
                self.assertEqual(_manifest_source(updated, "extension.yaml"), updated)
                for word in ("preserve", "first", "second", "third"):
                    if word in source:
                        self.assertIn(word, updated)

    def test_ambiguous_capability_aliases_and_duplicate_keys_block(self):
        for source in (
            'kind: Extension\ncapabilities: &caps [native.adk]\n',
            'kind: Extension\ncapabilities: [native.adk]\ncapabilities: []\n',
        ):
            with self.subTest(source=source), self.assertRaises(UpgradeError):
                _manifest_source(source, "extension.yaml")
