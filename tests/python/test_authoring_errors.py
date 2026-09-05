"""Verify actionable diagnostics without weakening feature discovery."""

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from harnest import Agent, BundleConventionError, bundle_agent
from harnest._library import _AuthoredNamespace, _validate_namespace_file
from harnest.bundle import (
    _discover_evals, _discover_sandboxes, _eval_file_kind, _load_export,
    _nested_subagent_entry, _resource_files, _skill_directories,
    _subagent_entry, _validate_resource_name,
)
from harnest.cli import main
from harnest.extension_loader import _extension_files
from harnest.plugin import _discover_agent_plugin, _discover_skill_directories
from harnest.runtime_plugins import _plugin_directories
from harnest.skills import _validate_filesystem_directory
from _session_store_fixture import write_session_store


class AuthoringErrorTests(unittest.TestCase):
    def setUp(self):
        """Keep invalid authored inputs isolated from the working project."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, relative, text="notes\n"):
        """Create a test-owned resource at the requested relative path."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def diagnostic(self, operation, *args, **kwargs):
        """Require a diagnosis, expected layout, and concrete repair instructions."""
        with self.assertRaises((ValueError, RuntimeError)) as caught:
            operation(*args, **kwargs)
        message = str(caught.exception)
        self.assertIn("What Harnest expects:", message)
        self.assertIn("How to fix:", message)
        return message

    def test_python_feature_folders_explain_notes_and_ignored_names(self):
        for kind in ("tools", "tasks", "cron", "mcp", "sandbox"):
            with self.subTest(kind=kind):
                path = self.write(f"{kind}/notes.txt")
                message = self.diagnostic(_resource_files, path.parent, kind=kind)
                self.assertIn(str(path), message)
                self.assertIn("_notes.txt", message)
                self.assertIn("automatic discovery", message)
                path.rename(path.with_name("_notes.txt"))
                self.assertEqual(_resource_files(path.parent, kind=kind), ())

    def test_folder_owned_resources_explain_where_files_belong(self):
        operations = (
            ("subagents", lambda path: _subagent_entry(path)),
            ("skills", lambda path: _skill_directories(path.parent)),
            ("plugins", lambda path: _discover_agent_plugin(path)),
            ("runtime_plugins", lambda path: _plugin_directories(path.parent)),
            ("evals", lambda path: _eval_file_kind(path)),
            ("extensions", lambda path: _extension_files(path.parent)),
        )
        for folder, operation in operations:
            with self.subTest(folder=folder):
                path = self.write(f"{folder}/notes.txt")
                message = self.diagnostic(operation, path)
                self.assertIn(str(path), message)
                self.assertIn("_notes.txt", message)

    def test_library_and_models_explain_non_python_placeholders(self):
        for folder in ("lib", "models"):
            path = self.write(f"{folder}/notes.md")
            binding = _AuthoredNamespace(folder, f"authored {folder}")
            message = self.diagnostic(_validate_namespace_file, path, binding)
            self.assertIn("reusable Python", message)
            self.assertIn("_notes.md", message)
            self.assertEqual(_validate_namespace_file(path.with_name("_notes.md"), binding), 0)

    def test_sandbox_missing_named_declaration_explains_safe_repair(self):
        path = self.write("sandbox/backup.py", "# unused\n")
        message = self.diagnostic(_discover_sandboxes, path.parent)
        self.assertIn(str(path.parent), message)
        self.assertIn("backup.py", message)
        self.assertIn("must export 'backup'", message)
        self.assertIn("lib/", message)
        self.assertIn("Name the intended declaration 'backup'", message)

    def test_sandbox_wrong_value_explains_required_assignment(self):
        path = self.write("sandbox/sandbox.py", "sandbox = {}\n")
        message = self.diagnostic(_discover_sandboxes, path.parent)
        self.assertIn("got dict", message)
        self.assertIn("sandbox = Sandbox.provider", message)

    def test_missing_export_explains_python_term_without_echoing_values(self):
        path = self.write("tools/search.py", "other = 'private-test-value'\n")
        message = self.diagnostic(_load_export, path, "search")
        self.assertIn("must export 'search'", message)
        self.assertIn("function or variable named 'search'", message)
        self.assertIn("what 'export' means", message)
        self.assertNotIn("private-test-value", message)

    def test_invalid_name_has_readable_example(self):
        path = self.root / "tools" / "search-customer.py"
        message = self.diagnostic(_validate_resource_name, path.stem, path)
        self.assertIn("without spaces or hyphens", message)
        self.assertIn("search_customer.py", message)

    def test_missing_nested_agent_names_the_file_to_create(self):
        path = self.root / "subagents" / "researcher"
        path.mkdir(parents=True)
        message = self.diagnostic(_nested_subagent_entry, path)
        self.assertIn(str(path / "agent.py"), message)
        self.assertIn("_researcher", message)

    def test_missing_skill_manifest_explains_case_and_location(self):
        path = self.root / "skills" / "research"
        path.mkdir(parents=True)
        for operation, target in (
            (_validate_filesystem_directory, path),
            (_discover_skill_directories, path.parent),
        ):
            message = self.diagnostic(operation, target)
            self.assertIn(str(path / "SKILL.md"), message)
            self.assertIn("skill.md", message)

    def test_eval_config_without_cases_explains_next_step(self):
        path = self.write("evals/test_config.json", "{}")
        message = self.diagnostic(_discover_evals, path.parent)
        self.assertIn("not a set of test cases", message)
        self.assertIn("quality.evalset.json", message)
        self.assertIn("_test_config.json", message)

    def test_public_bundle_entrypoint_preserves_guidance_and_rejects_file(self):
        anchor = self.write("agent.py", "# anchor\n")
        self.write("instructions.md", "Be useful.\n")
        bad = self.write("sandbox/README.md")
        definition = Agent(name="test", model="unused")
        message = self.diagnostic(bundle_agent, anchor, definition)
        self.assertIn(str(bad), message)
        self.assertIn("_README.md", message)
        self.assertTrue(bad.exists(), "diagnostics must not change user files")

    def test_cli_compile_prints_guidance_and_exits_unsuccessfully(self):
        write_session_store(self.root)
        self.write("agent.py", "from harnest import Agent\nroot_agent = Agent(name='test', model='unused')\n")
        self.write("instructions.md", "Be useful.\n")
        self.write("sandbox/notes.txt")
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = main(["compile", str(self.root), "--output", str(self.root / ".harnest" / "artifact")])
        self.assertNotEqual(status, 0)
        self.assertIn("What Harnest expects:", stderr.getvalue())
        self.assertIn("_notes.txt", stderr.getvalue())

    def test_ignored_symlinks_still_fail_validation(self):
        path = self.root / "tools" / "_backup.py"
        path.parent.mkdir()
        path.symlink_to(self.root / "outside.py")
        with self.assertRaisesRegex(BundleConventionError, "symlink"):
            _resource_files(path.parent, kind="tools")
