"""Framework API migration preserves source and blocks uncertain semantic changes."""

import unittest

from harnest.upgrade import UpgradeError
from harnest.upgrade_framework_source import framework_source_exports, migrate_framework_source


class FrameworkSourceMigrationTests(unittest.TestCase):
    def migrate(self, source, framework="adk"):
        result = migrate_framework_source(source, framework=framework, path="lib/agent.py")
        self.assertEqual(migrate_framework_source(result, framework=framework, path="lib/agent.py"), result)
        return result

    def test_import_aliases_decorators_and_comment_literals(self):
        source = ('from harnest.lifecycle import adk_plugin as plugin, native_extension\n'
                  '# adk_plugin native_extension stay literal\n'
                  'text = "native.extension"\n'
                  '@plugin(order=2)\ndef first(): pass\n'
                  '@native_extension\ndef second(): pass\n')
        changed = self.migrate(source)
        self.assertIn('framework_extension as plugin, framework_extension as native_extension', changed)
        self.assertIn('# adk_plugin native_extension stay literal', changed)
        self.assertIn('text = "native.extension"', changed)
        self.assertIn('@plugin(order=2)', changed)

    def test_mixed_moved_imports_keep_comments_and_names(self):
        source = ('from harnest.checkpoint import (\n'
                  '    ADKStore as Storage,  # advanced authority\n'
                  '    MemoryStore,\n'
                  '    LangGraphStore, # alternate authority\n'
                  ')\n')
        changed = self.migrate(source)
        self.assertIn('from harnest.frameworks.adk import ADKStore as Storage', changed)
        self.assertIn('from harnest.checkpoint import MemoryStore', changed)
        self.assertIn('from harnest.frameworks.langgraph import LangGraphStore', changed)
        self.assertIn('# advanced authority', changed)
        self.assertIn('# alternate authority', changed)

    def test_module_aliases_relocate_symbols_in_their_original_scope(self):
        source = ('def load():\n'
                  '    import harnest.checkpoint as cp # optional import\n'
                  '    return cp.ADKStore(service), cp.LangGraphStore(saver)\n')
        changed = self.migrate(source)
        self.assertIn('    import harnest.checkpoint as cp; from harnest.frameworks.', changed)
        self.assertIn('# optional import', changed)
        self.assertNotIn('cp.ADKStore', changed)
        self.assertNotIn('cp.LangGraphStore', changed)

    def test_model_factories_assignments_and_aliases(self):
        source = ('from harnest.model import LiteLLMModel as Model\n'
                  'model = Model.from_openai_environment()\n'
                  'alias = model\n'
                  'first = alias.build_for(framework="adk")\n'
                  'second = Model("provider/model").build("adk")\n')
        changed = self.migrate(source)
        self.assertIn('alias.build()', changed)
        self.assertIn('Model("provider/model").build()', changed)

    def test_langgraph_builder_requires_matching_selection(self):
        source = 'from harnest.model import LiteLLMModel\nmodel = LiteLLMModel("x/y").build_langgraph()\n'
        self.assertIn('.build()', self.migrate(source, "langgraph"))
        with self.assertRaisesRegex(UpgradeError, 'conflicts with project selection'):
            self.migrate(source)

    def test_annotated_receivers_and_factory_return_types(self):
        source = ('from harnest.model import LiteLLMModel\n'
                  'from typing import Optional\n'
                  'def factory() -> LiteLLMModel: return unknown()\n'
                  'first = factory().build_for("adk")\n'
                  'def use(model: "LiteLLMModel", other: Optional[LiteLLMModel]):\n'
                  '    return model.build_for("adk"), other.build("adk")\n')
        changed = self.migrate(source)
        self.assertIn('factory().build()', changed)
        self.assertIn('return model.build(), other.build()', changed)
        self.assertIn('model: "LiteLLMModel"', changed)

    def test_graph_keyword_selector_and_mcp_constructor(self):
        source = ('from harnest.graph import Graph\nfrom harnest.mcp import MCPClient\n'
                  'def run(graph: Graph):\n    return graph.build(backend="adk")\n'
                  'client = MCPClient.streamable_http("https://example.invalid")\n'
                  'options = client.to_langgraph_connection()\n'
                  'connection = client.connect(framework="adk")\n')
        changed = self.migrate(source)
        self.assertIn('graph.build()', changed)
        self.assertIn('client.connection_options()', changed)
        self.assertIn('client.connect()', changed)

    def test_selector_removal_retains_comments_and_unicode(self):
        source = ('import harnest as h\n'
                  'é = "private"; value = h.model.resolve_model_for(\n'
                  '    connector, # keep ownership\n'
                  '    framework="adk", # keep selection rationale\n'
                  ')\n')
        changed = self.migrate(source)
        self.assertIn('# keep ownership', changed)
        self.assertIn('# keep selection rationale', changed)
        self.assertIn('é = "private"', changed)
        self.assertNotIn('framework="adk"', changed)
        self.assertIn('h.model.resolve_model(', changed)

    def test_checkpoint_helper_moves_to_store_without_losing_comments(self):
        source = ('from harnest.frameworks.langgraph import checkpointer as saver\n'
                  'value = saver(\n    store, # ownership\n)\n')
        changed = self.migrate(source, "langgraph")
        self.assertNotIn('import checkpointer', changed)
        self.assertIn('# ownership', changed)
        self.assertIn(').framework_checkpointer()', changed)
        self.assertNotIn('store,', changed)

    def test_store_method_references_and_calls(self):
        source = ('from harnest.store import MemoryStore\nstore = MemoryStore()\n'
                  'factory = store.as_langgraph_checkpointer\n'
                  'value = store.as_langgraph_checkpointer()\n')
        changed = self.migrate(source, "langgraph")
        self.assertIn('factory = store.framework_checkpointer', changed)
        self.assertIn('value = store.framework_checkpointer()', changed)

    def test_server_and_compiled_application_keywords_and_fields(self):
        source = ('from harnest.runtime import create_fastapi_app\n'
                  'from harnest.application import CompiledApplication as App\n'
                  'server = create_fastapi_app(path, adk_session_storage=storage)\n'
                  'app = App(name="root", framework="adk", mode="managed", target=target, native_app=target)\n'
                  'target = app.native_app\napp.native_extensions = extensions\n')
        changed = self.migrate(source)
        self.assertIn('framework_session_storage=storage', changed)
        self.assertIn('framework_app=target', changed)
        self.assertIn('target = app.framework_app', changed)
        self.assertIn('app.framework_extensions = extensions', changed)

    def test_durable_python_fields_change_but_wire_strings_do_not(self):
        source = ('from harnest.durable import ResumeArtifact, NativeResumeInput\n'
                  'record = ResumeArtifact(framework="adk", native_invocation_id="id", tool_call_id="call", tool_name="tool")\n'
                  'value = record.native_invocation_id\n'
                  'wire = {"native_invocation_id": "id"}\n')
        changed = self.migrate(source)
        self.assertIn('FrameworkResumeInput as NativeResumeInput', changed)
        self.assertIn('framework_invocation_id="id"', changed)
        self.assertIn('value = record.framework_invocation_id', changed)
        self.assertIn('wire = {"native_invocation_id": "id"}', changed)

    def test_unrelated_classes_and_shadowed_parameters_stay_unchanged(self):
        source = ('from harnest.model import LiteLLMModel\nmodel = LiteLLMModel("x/y")\n'
                  'class Other:\n    def build_for(self, name): return name\n'
                  'other = Other()\nresult = other.build_for("adk")\n'
                  'def f(model): return model.build_for("adk")\n'
                  'callback = lambda model: model.build_for("adk")\n'
                  'values = [model.build_for("adk") for model in unrelated]\n'
                  'from unrelated import store\nstore.as_langgraph_checkpointer()\n')
        self.assertEqual(self.migrate(source), source)

    def test_custom_connector_overrides_block(self):
        for signature in ('build_for(self, framework)', 'build_langgraph(self)',
                          'build(self, framework)', 'build(self, *, framework="adk")',
                          'build(self, **kwargs)', 'build(self, *args)'):
            with self.subTest(signature=signature), self.assertRaisesRegex(UpgradeError, 'custom ModelConnector'):
                self.migrate('from harnest.model import ModelConnector\nclass Custom(ModelConnector):\n'
                             f'    def {signature}: pass\n')

    def test_dynamic_mismatched_and_duplicate_selectors_block_without_values(self):
        for arguments in ('secret_value', '"private-mismatch"', '"adk", framework="adk"', '**secret_options'):
            source = 'from harnest.model import LiteLLMModel\nvalue = LiteLLMModel("x/y").build_for('+arguments+')\n'
            with self.subTest(arguments=arguments), self.assertRaises(UpgradeError) as caught:
                self.migrate(source)
            self.assertIn('lib/agent.py:2:', str(caught.exception))
            self.assertNotIn('secret_value', str(caught.exception))
            self.assertNotIn('private-mismatch', str(caught.exception))
            self.assertNotIn('secret_options', str(caught.exception))

    def test_conflicting_renamed_keywords_block(self):
        source = ('from harnest.runtime import create_fastapi_app\n'
                  'create_fastapi_app(path, adk_session_storage=one, framework_session_storage=two)\n')
        with self.assertRaisesRegex(UpgradeError, 'conflicting'):
            self.migrate(source)

    def test_removed_helpers_and_private_modules_block(self):
        sources = ('from harnest.runtime_adk import ADKRuntimeDriver\n',
                   'from harnest.durable import adk_durable_tool\n',
                   'from harnest.mcp import MCPClient\nMCPClient.stdio("cmd").to_adk_toolset()\n',
                   'from harnest.sandbox import Sandbox\ndef f(value: Sandbox): return value.to_adk_executor()\n')
        for source in sources:
            with self.subTest(source=source), self.assertRaises(UpgradeError):
                self.migrate(source)

    def test_ambiguous_harnest_receivers_and_captured_helpers_block(self):
        sources = ('from harnest.lib.models import model\nmodel.build_for("adk")\n',
                   'from harnest.model import LiteLLMModel\n'
                   'model = LiteLLMModel("x/y") if flag else unknown\nmodel.build_for("adk")\n',
                   'from harnest.model import LiteLLMModel\nmodel = LiteLLMModel("x/y")\ncallback = model.build_for\n',
                   'from harnest.frameworks.langgraph import checkpointer\ncallback = checkpointer\n')
        for source in sources:
            with self.subTest(source=source), self.assertRaises(UpgradeError):
                self.migrate(source)

    def test_lexical_shadowing_in_imports_loops_contexts_and_destructuring(self):
        prefix = 'from harnest.model import LiteLLMModel as model\n'
        bodies = (
            'def use():\n    model.build_for("adk")\n    import unrelated as model\n',
            'for model in others:\n    model.build_for("adk")\n',
            'with opaque() as model:\n    model.build_for("adk")\n',
            'model, other = opaque()\nmodel.build_for("adk")\n',
            'try:\n    pass\nexcept Exception as model:\n    model.build_for("adk")\n',
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.migrate(prefix + body), prefix + body)

    def test_uncertain_loop_and_exception_flows_block(self):
        prefix = 'from harnest.model import LiteLLMModel\nmodel = LiteLLMModel("x/y")\n'
        flows = ('for item in others:\n    model = opaque()\n',
                 'while condition:\n    model = opaque()\n',
                 'try:\n    model = opaque()\nexcept Exception:\n    pass\n')
        for flow in flows:
            with self.subTest(flow=flow), self.assertRaisesRegex(UpgradeError, 'ambiguous'):
                self.migrate(prefix + flow + 'model.build_for("adk")\n')

    def test_moved_import_comments_never_hide_following_statements(self):
        source = ('from harnest.checkpoint import (\n'
                  '    ADKStore, # retained comment\n'
                  '); must_run()\n')
        with self.assertRaisesRegex(UpgradeError, 'shares a line'):
            self.migrate(source)

    def test_capability_members_and_constructor_aliases(self):
        source = ('from harnest.frameworks import Capability as Cap\n'
                  'import harnest.frameworks.base as base\n'
                  'Kind = Cap\n'
                  'first = Cap.NATIVE_API_SERVER\n'
                  'second = base.Capability.NATIVE_SESSION_SERVICE\n'
                  'third = Kind("native-api-server")\n'
                  'fourth = Cap(value="native" "-session-service")\n'
                  'wire = "native-api-server"\n')
        changed = self.migrate(source)
        self.assertIn('Cap.FRAMEWORK_API_SERVER', changed)
        self.assertIn('base.Capability.FRAMEWORK_SESSION_SERVICE', changed)
        self.assertIn('Kind("framework-api-server")', changed)
        self.assertIn("Cap(value='framework-session-service')", changed)
        self.assertIn('wire = "native-api-server"', changed)
        with self.assertRaisesRegex(UpgradeError, 'dynamic capability'):
            self.migrate('from harnest.frameworks import Capability\nCapability(secret)\n')

    def test_adapter_calls_and_legacy_custom_overrides(self):
        source = ('from harnest.frameworks import FrameworkAdapter, get_framework\n'
                  'def use(adapter: FrameworkAdapter):\n'
                  '    return adapter.native_extension_type(), adapter.host_sessions(a, b, native_storage=s)\n'
                  'get_framework("adk").build_native_server(a, b)\n')
        changed = self.migrate(source)
        self.assertIn('adapter.framework_extension_type()', changed)
        self.assertIn('framework_storage=s', changed)
        self.assertIn('.build_framework_server(a, b)', changed)
        overrides = ('    native_extension_label = "extension"\n',
                     '    def native_extension_type(self): pass\n',
                     '    def lower_agent(self, value, *, native_extensions): pass\n')
        for override in overrides:
            with self.subTest(override=override), self.assertRaisesRegex(UpgradeError, 'custom FrameworkAdapter'):
                self.migrate('from harnest.frameworks import FrameworkAdapter\nclass Custom(FrameworkAdapter):\n' + override)

    def test_class_names_do_not_leak_into_method_lexical_scope(self):
        source = ('from harnest.model import LiteLLMModel as Model\n'
                  'class Consumer:\n'
                  '    Model = unrelated\n'
                  '    def use(self): return Model("x/y").build_for("adk")\n')
        self.assertIn('return Model("x/y").build()', self.migrate(source))
        unrelated = ('from unrelated import Model\n'
                     'class Consumer:\n'
                     '    from harnest.model import LiteLLMModel as Model\n'
                     '    def use(self): return Model("x/y").build_for("adk")\n')
        self.assertEqual(self.migrate(unrelated), unrelated)

    def test_cross_module_model_aliases_relative_imports_and_factory_exports(self):
        definition = ('from harnest.model import LiteLLMModel as Model\n'
                      'model = Model.from_openai_environment()\n'
                      'def factory() -> Model: raise RuntimeError("never run")\n')
        exports = framework_source_exports(definition, module="harnest.models.default")
        imported = {f"harnest.models.default.{name}": owner for name, owner in exports.items()}
        bridge = 'from ..models.default import model as primary\nalias = primary\n'
        bridge_exports = framework_source_exports(bridge, imported_types=imported, module="harnest.lib.__init__")
        imported.update({f"harnest.lib.{name}": owner for name, owner in bridge_exports.items()})
        source = ('from . import alias\n'
                  'from harnest.models.default import factory, Model\n'
                  'alias.build_for("adk")\n'
                  'factory().build("adk")\n'
                  'Model.from_openai_environment().build_for("adk")\n')
        changed = migrate_framework_source(source, framework="adk", path="lib/agent.py",
                                           imported_types=imported, module="harnest.lib.agent")
        self.assertNotIn('build_for', changed)
        self.assertNotIn('"adk"', changed)

    def test_ordinary_imports_preserve_conflicting_harnest_ownership(self):
        definitions = (
            'from harnest.store import MemoryStore\nstore = MemoryStore() if condition else unrelated\n',
            'from harnest.lib.storage import store\nstore.as_langgraph_checkpointer()\n',
        )
        for definition in definitions:
            with self.subTest(definition=definition):
                exports = framework_source_exports('raise RuntimeError("never run")\n' + definition)
                imported = {f"lib.storage.{name}": owner for name, owner in exports.items()}
                bridge = framework_source_exports('from .storage import store as shared\n',
                                                   imported_types=imported, module="lib.bridge")
                imported.update({f"lib.bridge.{name}": owner for name, owner in bridge.items()})
                with self.assertRaisesRegex(UpgradeError, 'ambiguous Harnest receiver'):
                    migrate_framework_source('from lib.bridge import shared\nshared.as_langgraph_checkpointer()\n',
                                             framework="langgraph", path="agent.py", imported_types=imported)

if __name__ == '__main__':
    unittest.main()
