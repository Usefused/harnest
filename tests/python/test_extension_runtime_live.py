"""Run the canonical layout through real ADK/LangGraph HTTP runtime boundaries."""

from pathlib import Path
import textwrap

import test_runtime_plugin_adk_live as adk
import test_runtime_plugin_live_integration as langgraph


def canonical_path(path: Path) -> Path:
    """Translate only package-layout segments in the inherited authored fixture."""

    parts = tuple("lifecycle" if part == "extensions" else
                  "extensions" if part == "plugins" else part for part in path.parts)
    return Path(*parts).with_name(path.name.replace("plugin.yaml", "extension.yaml")
                                 .replace("plugin.py", "extension.py"))


def canonical_source(path: Path, value: str) -> str:
    """Exercise canonical APIs while retaining the legacy singleton alias for assertions."""

    source = textwrap.dedent(value).lstrip()
    source = source.replace("kind: RuntimePlugin", "kind: Extension")
    source = source.replace("entrypoint: plugin:plugin", "entrypoint: extension:extension")
    source = source.replace("from harnest.plugins import Plugin, PluginContext",
                            "from harnest.extensions import Extension as Plugin, ExtensionContext as PluginContext")
    source = source.replace("context.plugins(", "context.extensions(")
    if path.name == "extension.py":
        source += "\nextension = plugin\n"
    return source


class CanonicalADKExtensionTests(adk.RuntimePluginADKLiveTests):
    def _write(self, root, relative, contents):
        """Use new layout and API with the existing full HTTP ownership assertions."""

        path = canonical_path(Path(relative))
        super()._write(root, str(path), canonical_source(path, contents))


class CanonicalLangGraphExtensionTests(langgraph.RuntimePluginLiveIntegrationTests):
    def _write(self, path, value):
        """Use new layout and API with real tool, stdio MCP, and HTTP transport."""

        target = canonical_path(path)
        super()._write(target, canonical_source(target, value))

    def _write_root(self, root):
        """Move the inherited legacy storage fixture before authoring new packages."""

        super()._write_root(root)
        (root / "extensions").rename(root / "lifecycle")
