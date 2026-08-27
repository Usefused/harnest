import tempfile
import unittest
from pathlib import Path

from harnest.plugin import (
    PluginConventionError,
    PluginResources,
    discover_plugins,
)


class PluginDiscoveryTests(unittest.TestCase):
    def _write(self, path: Path, contents: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def test_discovers_mcp_clients_and_skills_without_aggregation_module(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(plugins / "support" / "mcp" / "knowledge.py")
            self._write(plugins / "support" / "mcp" / "tickets.py")
            self._write(
                plugins / "support" / "skills" / "triage" / "SKILL.md",
                "# Triage\n",
            )

            result = discover_plugins(plugins)

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], PluginResources)
            self.assertEqual(result[0].name, "support")
            self.assertEqual(
                [path.relative_to(plugins).as_posix() for path in result[0].mcp_sources],
                [
                    "support/mcp/knowledge.py",
                    "support/mcp/tickets.py",
                ],
            )
            self.assertEqual(
                [
                    path.relative_to(plugins).as_posix()
                    for path in result[0].skill_directories
                ],
                ["support/skills/triage"],
            )

    def test_orders_plugins_and_resources_by_directory_name(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            for plugin_name in ("zeta", "alpha"):
                for client_name in ("zulu", "able"):
                    self._write(
                        plugins / plugin_name / "mcp" / f"{client_name}.py"
                    )
                self._write(
                    plugins / plugin_name / "skills" / "usage" / "SKILL.md"
                )

            result = discover_plugins(plugins)

            self.assertEqual([plugin.name for plugin in result], ["alpha", "zeta"])
            self.assertEqual(
                [path.stem for path in result[0].mcp_sources], ["able", "zulu"]
            )

    def test_skips_missing_and_empty_plugin_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(discover_plugins(root / "missing"), ())
            (root / "plugins" / "empty" / "mcp").mkdir(parents=True)
            (root / "plugins" / "ignored" / "_notes").mkdir(parents=True)

            self.assertEqual(discover_plugins(root / "plugins"), ())

    def test_rejects_python_aggregation_file_at_plugin_root(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(plugins / "support" / "plugin.py", "support = object()\n")

            with self.assertRaisesRegex(
                PluginConventionError, "only mcp/ and skills/"
            ):
                discover_plugins(plugins)

    def test_rejects_incomplete_plugin_capability(self):
        for resource in ("mcp", "skills"):
            with self.subTest(resource=resource), tempfile.TemporaryDirectory() as directory:
                plugins = Path(directory) / "plugins"
                if resource == "mcp":
                    self._write(plugins / "support" / "mcp" / "support.py")
                else:
                    self._write(
                        plugins / "support" / "skills" / "usage" / "SKILL.md"
                    )
                with self.assertRaisesRegex(
                    PluginConventionError, "must combine at least one MCP client"
                ):
                    discover_plugins(plugins)

    def test_rejects_agent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(plugins / "support" / "agents" / "researcher.py")

            with self.assertRaisesRegex(
                PluginConventionError, "only mcp/ and skills/"
            ):
                discover_plugins(plugins)

    def test_rejects_malformed_mcp_and_skill_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(plugins / "support" / "mcp" / "bad-name.py")
            with self.assertRaisesRegex(
                PluginConventionError, "public Python module"
            ):
                discover_plugins(plugins)

        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            (plugins / "support" / "skills" / "triage").mkdir(parents=True)
            with self.assertRaisesRegex(
                PluginConventionError, "uppercase SKILL.md"
            ):
                discover_plugins(plugins)

    def test_rejects_nested_mcp_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(plugins / "support" / "mcp" / "nested" / "client.py")

            with self.assertRaisesRegex(
                PluginConventionError, "public Python module"
            ):
                discover_plugins(plugins)

    def test_rejects_symlinks_even_when_the_name_would_be_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins = root / "plugins"
            self._write(root / "outside.py")
            mcp = plugins / "support" / "mcp"
            mcp.mkdir(parents=True)
            (mcp / "_helper.py").symlink_to(root / "outside.py")

            with self.assertRaisesRegex(PluginConventionError, "cannot be a symlink"):
                discover_plugins(plugins)

    def test_plugin_name_is_a_filesystem_slug_not_a_python_export(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            self._write(
                plugins / "customer-support" / "skills" / "triage" / "SKILL.md",
                "# Triage\n",
            )
            self._write(
                plugins / "customer-support" / "mcp" / "customer_support.py"
            )

            self.assertEqual(discover_plugins(plugins)[0].name, "customer-support")


if __name__ == "__main__":
    unittest.main()
