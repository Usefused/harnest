"""Compile manifests cross the authoring, artifact, and pack ownership boundaries."""
import json
from pathlib import Path
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

from harnest.bundle import BundleConventionError, compile_application, compile_artifact
from harnest.compile_selection import read_compile_selection
from test_authoring import _fake_adk_modules
from _session_store_fixture import write_session_store


class CompileSelectionTests(unittest.TestCase):
    """Exercise declarations through actual artifact construction, without providers."""

    def setUp(self):
        """Build an isolated source tree with selected and unselected dependencies."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.root.mkdir()
        (self.root / "instructions.md").write_text("Test instructions")
        (self.root / "harnest.lock").write_text("apiVersion: harnest.dev/v1alpha1\nkind: ProjectLock\nprojectSchema: 0\n")
        self.output = self.root.parent / "artifact"
        write_session_store(self.root)
        (self.root / "agent.py").write_text("from harnest.agent import Agent\nroot_agent = Agent(name='root', model='gemini-test', instruction='Test instructions')\n")
        (self.root / "pyproject.toml").write_text("[project]\nname='test'\nversion='1.0'\ndependencies=[]\n[project.optional-dependencies]\ncrm=['company-sdk==2']\nunused=['never-install-unused==1']\n")
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "system.txt").write_text("company instructions")
        self.manifest = self.root / "harnest-compile.yaml"
        self.manifest.write_text("version: 1\nextras: [crm]\n")

    def compile(self):
        """Run the real compiler with an offline framework adapter fixture."""
        with patch.dict(sys.modules, _fake_adk_modules()):
            return compile_artifact(self.root, self.output)

    def test_report_and_digest_are_deterministic(self):
        """Reports retain actual runtime files and selected dependency roots."""
        first = self.compile()
        self.assertEqual(first, self.compile())
        report = json.loads((self.output / 'harnest-build-report.json').read_text())
        paths = [item['path'] for item in report['files']]
        self.assertIn('source/instructions.md', paths)
        self.assertIn('source/harnest.lock', paths)
        self.assertNotIn('source/prompts/system.txt', paths)
        self.assertNotIn('resources', report['selection'])
        self.assertEqual(report['selectedRequirements'], ['company-sdk==2'])
        self.assertEqual(report['logicalBytes'], sum(item['size'] for item in report['files']))
        (self.root / 'instructions.md').write_text('changed instructions')
        self.assertNotEqual(first['digest'], self.compile()['digest'])

    def test_invalid_selection_fails_before_import_and_preserves_output(self):
        """Unsupported resource declarations fail before side effects or artifact replacement."""
        self.compile()
        before = (self.output / 'harnest-manifest.json').read_bytes()
        self.manifest.write_text('version: 1\nresources: [docs]\n')
        (self.root / 'agent.py').write_text("raise AssertionError('must not import')\n")
        with self.assertRaisesRegex(BundleConventionError, 'unknown'):
            self.compile()
        self.assertEqual(before, (self.output / 'harnest-manifest.json').read_bytes())

    def test_invalid_manifest_is_rejected(self):
        """Reject ambiguous documents and paths consistently with the native planner."""
        for body in ('version: 2', 'version: true', 'version: 1\nextra: []', 'version: 1\nextras: null', 'version: 1\nextras: [1]', 'version: 1\nresources: [../secret]', 'version: 1\nversion: 1', 'version: 1\n---\nversion: 1', 'version: 1\nextras: [missing]'):
            with self.subTest(body=body):
                self.manifest.write_text(body)
                with self.assertRaises((ValueError, BundleConventionError)):
                    read_compile_selection(self.root)

    def test_source_run_ignores_compile_declarations(self):
        """The internal source-run path ignores compile-only dependency and content rules."""
        self.manifest.write_text("version: 99\nresources: [does-not-exist]\n")
        with patch.dict(sys.modules, _fake_adk_modules()):
            compile_artifact(self.root, self.output, bundle_selection=False)
        self.assertFalse((self.output / 'harnest-build-report.json').exists())
        self.assertTrue((self.output / 'source/prompts/system.txt').is_file())

    def test_unselected_content_stays_in_project_and_out_of_artifact(self):
        """Compilation does not turn the repository's guides into runtime content."""
        guide = self.root / 'team-guide.md'
        guide.write_text('Read this when setting up your workspace.')
        for manifest in (None, 'version: 1\n'):
            with self.subTest(manifest=manifest):
                self.manifest.unlink(missing_ok=True)
                if manifest is not None:
                    self.manifest.write_text(manifest)
                first = self.compile()
                self.assertTrue(guide.is_file())
                self.assertFalse((self.output / 'source/team-guide.md').exists())
                self.assertFalse((self.output / 'source/prompts/system.txt').exists())
                self.assertTrue((self.output / 'source/instructions.md').is_file())
                guide.write_text(guide.read_text() + '\nMore team setup guidance.')
                self.assertEqual(first['digest'], self.compile()['digest'])

    def test_instruction_and_skill_templates_compile_without_resources(self):
        """Conventional agent guidance survives independently of compile declarations."""
        self.manifest.write_text('version: 1\n')
        skill = self.root / 'skills/team-help'
        skill.mkdir(parents=True)
        (skill.parent / '_README.md').write_text('Guide for teammates adding skills.')
        sample = skill.parent / '_example'
        sample.mkdir()
        (sample / 'SKILL.md').write_text('Inactive authoring template.')
        (skill / 'SKILL.md').write_text('---\nname: team-help\ndescription: Help with team tasks.\n---\nRead reference.md for details.')
        (skill / 'reference.md').write_text('Agent-facing reference.')
        self.compile()
        self.assertEqual((self.output / 'source/skills/team-help/reference.md').read_text(), 'Agent-facing reference.')
        self.assertFalse((self.output / 'source/skills/_README.md').exists())
        self.assertFalse((self.output / 'source/skills/_example/SKILL.md').exists())
        self.assertTrue((self.output / 'source/instructions.md').is_file())
        # Runtime loading must use the artifact after the authoring project is gone.
        shutil.rmtree(self.root)
        with patch.dict(sys.modules, _fake_adk_modules()):
            built = compile_application(self.output / 'source', entrypoint='agent:root_agent')
        self.assertIn('root', built.skill_registry.agent_names)

    def test_resources_cannot_add_instructions_or_skills(self):
        """Reject the removed field for all content, including an empty list."""
        (self.root / 'prompts/instructions.md').write_text('Use a template instead.')
        for resource in ('instructions.md', 'skills/team-help', 'docs', 'lookup.json', ''):
            with self.subTest(resource=resource):
                self.manifest.write_text(f'version: 1\nresources: [{resource}]\n')
                with self.assertRaisesRegex(BundleConventionError, 'unknown'):
                    self.compile()
