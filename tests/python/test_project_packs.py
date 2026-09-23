"""Exercise real Harnest scaffolds, company migrations and CLI failure recovery."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel
import yaml

from harnest.authoring import ChangePlan, ProjectCLI, ProjectError, ProjectPack, ProjectPlanner, WritePolicy, apply_project_plan
from harnest.authoring.filesystem import atomic_write, snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples/project-packs/acme.py'
spec = importlib.util.spec_from_file_location('acme_pack_example', EXAMPLE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


_TEMPLATE_CLI = '''"""Stand in for the existing native template downloader at the process boundary."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('command', choices=['init'])
parser.add_argument('directory', type=Path)
parser.add_argument('--template', required=True)
parser.add_argument('--template-sha256')
args = parser.parse_args()
args.directory.mkdir(parents=True)
(args.directory / 'instructions.md').write_text('Template-owned instructions.')
if args.template == 'failed-template':
    parser.exit(7, 'template download failed\\n')
(args.directory / 'config.yaml').write_text('spec:\\n  framework:\\n    name: langgraph\\n    mode: managed\\n')
skill = args.directory / 'skills' / 'support'
skill.mkdir(parents=True)
(skill / 'SKILL.md').write_text('Template-owned skill.')
(args.directory / 'forwarded.json').write_text(json.dumps({
    'template': args.template, 'sha256': args.template_sha256, 'stage': str(args.directory),
}))
'''


class ProjectPackTemplateTests(unittest.TestCase):
    """Exercise forwarding and staged pack composition through an actual CLI subprocess."""

    def setUp(self):
        """Use a controlled scaffold process; native template wheel validation has Go coverage."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        self.root = workspace / 'team agent'
        fixture = workspace / 'template cli.py'
        fixture.write_text(_TEMPLATE_CLI)
        self.command = (sys.executable, str(fixture))
        self.pack = ProjectPack('team', 1)
        self.seen = []

        @self.pack.initialize
        def initialize(context):
            """Observe template content before proposing project-only team documentation."""
            self.seen.append(context.files.read_text('instructions.md'))
            return ChangePlan(context.files.write_text('docs/team-guide.md', 'Team setup guide.'))

    def test_planner_composes_template_before_pack_and_applies_snapshot(self):
        """A pinned URL is forwarded unchanged; one combined plan retains template settings."""
        reference = 'https://example.test/sales.whl?signature=a&version=1'
        planner = ProjectPlanner([self.pack], harnest_command=self.command)
        plan = planner.plan_init(self.root, template=reference, template_sha256='a' * 64)
        self.assertFalse(plan.blockers)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.seen, ['Template-owned instructions.'])
        apply_project_plan(plan)
        receipt = json.loads((self.root / 'forwarded.json').read_text())
        self.assertEqual(receipt['template'], reference)
        self.assertEqual(receipt['sha256'], 'a' * 64)
        self.assertFalse(Path(receipt['stage']).exists())
        self.assertEqual((self.root / 'docs/team-guide.md').read_text(), 'Team setup guide.')
        self.assertEqual((self.root / 'skills/support/SKILL.md').read_text(), 'Template-owned skill.')
        self.assertEqual(yaml.safe_load((self.root / 'config.yaml').read_text())['spec']['framework']['name'], 'langgraph')

    def test_cli_template_preview_and_apply(self):
        """The embedded CLI exposes template flags without sending its default framework."""
        cli = ProjectCLI('team-agent', [self.pack], harnest_command=self.command)
        flags = ['init', str(self.root), '--template', 'sales', '--template-sha256', 'b' * 64]
        output = io.StringIO()
        self.assertEqual(cli.run([*flags, '--dry-run', '--json'], stdout=output), 0)
        self.assertFalse(self.root.exists())
        self.assertIn('docs/team-guide.md', output.getvalue())
        self.assertEqual(cli.run(flags, stdout=io.StringIO()), 0)
        receipt = json.loads((self.root / 'forwarded.json').read_text())
        self.assertEqual(receipt['template'], 'sales')
        self.assertEqual(receipt['sha256'], 'b' * 64)
        self.assertEqual(json.loads((self.root / 'harnest-packs.lock').read_text())['packs'], {'team': 1})

    def test_template_pin_is_optional(self):
        """Named templates and URLs retain the native CLI's optional-pin behavior."""
        plan = ProjectPlanner([self.pack], harnest_command=self.command).plan_init(self.root, template='sales')
        apply_project_plan(plan)
        self.assertIsNone(json.loads((self.root / 'forwarded.json').read_text())['sha256'])

    def test_planner_rejects_conflicts_before_core_or_pack_execution(self):
        """Explicit choices cannot silently override a template or discard a checksum."""
        cases = (
            {'template': 'sales', 'framework': 'adk'},
            {'template': 'sales', 'framework': 'langgraph'},
            {'template': 'sales', 'minimal': True},
            {'template_sha256': 'a' * 64},
            {'template': ''},
            {'template': 'sales', 'template_sha256': ''},
        )
        planner = ProjectPlanner([self.pack], harnest_command=self.command)
        for options in cases:
            with self.subTest(options=options), patch('harnest.authoring.planner.subprocess.run') as core:
                with self.assertRaises(ProjectError):
                    planner.plan_init(self.root, **options)
                core.assert_not_called()
        self.assertEqual(self.seen, [])
        self.assertFalse(self.root.exists())

    def test_cli_rejects_template_conflicts(self):
        """CLI defaults are omitted but an explicitly supplied framework remains a conflict."""
        cli = ProjectCLI('team-agent', [self.pack], harnest_command=self.command)
        for flags in (['--template', 'sales', '--framework', 'adk'], ['--template', 'sales', '--minimal'], ['--template-sha256', 'a' * 64]):
            with self.subTest(flags=flags), patch('harnest.authoring.planner.subprocess.run') as core:
                errors = io.StringIO()
                self.assertEqual(cli.run(['init', str(self.root), *flags], stderr=errors), 1)
                self.assertIn('template', errors.getvalue())
                core.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_core_failure_leaves_target_untouched_and_skips_packs(self):
        """Partially downloaded or rendered templates cannot leak into the live destination."""
        cli = ProjectCLI('team-agent', [self.pack], harnest_command=self.command)
        errors = io.StringIO()
        self.assertEqual(cli.run(['init', str(self.root), '--template', 'failed-template'], stderr=errors), 1)
        self.assertIn('exit 7', errors.getvalue())
        self.assertEqual(self.seen, [])
        self.assertFalse(self.root.exists())


class ProjectPackIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / ('harnest.exe' if os.name == 'nt' else 'harnest')
        subprocess.run(['go', 'build', '-o', str(cls.binary), './cmd/harnest'], cwd=ROOT, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'support-bot'

    def planner(self, version=2, packs=None):
        return ProjectPlanner(packs if packs is not None else [example.create_pack(version)], harnest_command=(str(self.binary),))

    def initialize(self, version=1):
        plan = self.planner(version).plan_init(self.root, options={'acme': {'team': 'support'}}, minimal=True)
        self.assertEqual(plan.blockers, ())
        apply_project_plan(plan)
        return plan

    def test_init_preview_is_read_only_and_options_are_typed(self):
        plan = self.planner().plan_init(self.root, options={'acme': {'team': 'support', 'environment': 'production'}})
        self.assertFalse(self.root.exists())
        self.assertFalse(plan.blockers)
        apply_project_plan(plan)
        config = yaml.safe_load((self.root / 'acme-agent.yaml').read_text())
        self.assertEqual(config, {'team': 'support', 'environment': 'production'})
        self.assertTrue((self.root / 'agent.py').exists())
        self.assertNotIn('production', json.dumps(plan.public()))
        lock = json.loads((self.root / 'harnest-packs.lock').read_text())
        self.assertEqual(lock['packs'], {'acme': 2})
        self.assertNotIn('support', json.dumps(lock))

    def test_generated_pack_cli_creates_compile_ready_agent(self):
        """Keep generated teammate guides separate from the runnable agent artifact."""
        from harnest.bundle import compile_artifact
        pack_root = self.root.parent / 'company pack'
        subprocess.run([str(self.binary), 'pack', 'init', 'acme', '--output', str(pack_root)],
                       check=True, capture_output=True, text=True)
        (pack_root / 'team-guide.md').write_text('Team-owned documentation before init.\n')
        environment = dict(os.environ, HARNEST_CLI=str(self.binary), PYTHONPATH=str(ROOT / 'src'))
        subprocess.run([sys.executable, str(pack_root / 'pack.py'), 'init', str(self.root), '--minimal'],
                       env=environment, check=True, capture_output=True, text=True)
        self.assertEqual((self.root / 'harnest-compile.yaml').read_bytes(),
                         (pack_root / 'harnest-compile.yaml').read_bytes())
        self.assertEqual((self.root / 'docs/team-guide.md').read_text(), 'Team-owned documentation before init.\n')
        self.assertEqual(json.loads((self.root / 'harnest-packs.lock').read_text())['packs'], {'acme': 1})
        # Compilation must consume generated declarations, without the authoring package.
        shutil.rmtree(pack_root)
        target = self.root.parent / 'artifact'
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'http://localhost:11434/v1', 'OPENAI_MODEL': 'test-model'}):
            compile_artifact(self.root, target)
        report = json.loads((target / 'harnest-build-report.json').read_text())
        self.assertNotIn('resources', report['selection'])
        self.assertFalse((target / 'source/docs/team-guide.md').exists())
        self.assertTrue((self.root / 'docs/team-guide.md').is_file())
        self.assertFalse((target / 'source/pack.py').exists())

    def test_pack_documents_remain_in_project_without_entering_compiled_agent(self):
        """Pack documents retain exact bytes and ownership only in the generated project."""
        from harnest.bundle import compile_artifact
        assets = self.root.parent / 'pack-assets'
        assets.mkdir()
        content = b'%PDF-fixture\x00\xff\xfe\r\n'
        (assets / 'handbook.pdf').write_bytes(content)
        (assets / 'reference.md').write_text('Literal $EXAMPLE and ${VARIABLE} documentation.\n')
        pack = ProjectPack('references', 1, templates=assets)
        pack.initialize(lambda ctx: ChangePlan(
            ctx.files.from_file('docs/handbook.pdf', source='handbook.pdf'),
            ctx.files.from_file('docs/reference.md', source='reference.md'),
            ctx.yaml.set('harnest-compile.yaml', key=('version',), value=1),
        ))
        plan = self.planner(packs=[pack]).plan_init(self.root, minimal=True)
        self.assertFalse(plan.blockers)
        apply_project_plan(plan)
        self.assertEqual((self.root / 'docs/handbook.pdf').read_bytes(), content)
        self.assertEqual((self.root / 'docs/reference.md').read_bytes(), (assets / 'reference.md').read_bytes())
        lock = json.loads((self.root / 'harnest-packs.lock').read_text())
        self.assertTrue(any(item['owner'] == 'references' and item['path'] == 'docs/handbook.pdf' for item in lock['claims']))
        shutil.rmtree(assets)
        target = self.root.parent / 'compiled-documents'
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'http://localhost:11434/v1', 'OPENAI_MODEL': 'test-model'}):
            compile_artifact(self.root, target)
        self.assertEqual((self.root / 'docs/handbook.pdf').read_bytes(), content)
        self.assertFalse((target / 'source/docs').exists())
        report = json.loads((target / 'harnest-build-report.json').read_text())
        self.assertFalse(any(item['path'].startswith('source/docs/') for item in report['files']))

    def test_pack_file_copy_preserves_managed_update_checks(self):
        """Binary copying uses the same ownership and local-edit protections as text templates."""
        from harnest.authoring.context import ProjectFiles
        from harnest.authoring.contracts import _File
        from harnest.authoring.operations import OperationEngine, read_lock
        source = self.root.parent / 'document.bin'
        source.write_bytes(b'\xfforiginal')
        engine = OperationEngine({}, read_lock({}))
        files = ProjectFiles(engine.files, source.parent)
        engine.apply('references', files.from_file('docs/document.bin', source=source.name))
        source.write_bytes(b'\x00updated')
        engine.apply('references', files.from_file('docs/document.bin', source=source.name, policy=WritePolicy.MANAGED))
        self.assertEqual(engine.files['docs/document.bin'].content, b'\x00updated')
        engine.files['docs/document.bin'] = _File(b'user edits')
        with self.assertRaisesRegex(ProjectError, 'local modifications'):
            engine.apply('references', files.from_file('docs/document.bin', source=source.name, policy=WritePolicy.MANAGED))
        self.assertEqual(engine.files['docs/document.bin'].content, b'user edits')

    def test_pack_file_copy_rejects_escaping_and_non_file_sources(self):
        """Verbatim copies and templates share the pack's existing containment boundary."""
        from harnest.authoring.context import ProjectFiles
        assets = self.root.parent / 'assets'
        assets.mkdir()
        (assets / 'folder').mkdir()
        outside = self.root.parent / 'outside.txt'
        outside.write_text('outside')
        (assets / 'escape').symlink_to(outside)
        files = ProjectFiles({}, assets)
        for source in ('../outside.txt', 'escape', 'missing.txt', 'folder'):
            with self.subTest(source=source), self.assertRaises(ProjectError):
                files.from_file('docs/reference', source=source)
        with self.assertRaisesRegex(ProjectError, 'no template directory'):
            ProjectFiles({}, None).from_file('docs/reference', source='reference')

    def test_invalid_options_block_before_live_mutation(self):
        for values in [{}, {'team': ''}, {'team': 'ok', 'environment': 'bad'}, {'team': 'ok', 'unknown': 'x'}]:
            with self.subTest(values=values):
                plan = self.planner().plan_init(self.root, options={'acme': values})
                self.assertTrue(plan.blockers)
                with self.assertRaises(ProjectError):
                    apply_project_plan(plan)
                self.assertFalse(self.root.exists())

    def test_upgrade_combines_core_and_pack_migrations_and_is_repeatable(self):
        self.initialize()
        (self.root / 'harnest.lock').write_text('apiVersion: harnest.dev/v1alpha1\nkind: ProjectLock\nprojectSchema: 5\n')
        (self.root / 'acme-agent.yaml').write_text('owner: custom-team\nenvironment: development\ncustom: kept\n')
        before = snapshot(self.root)
        plan = self.planner().plan_upgrade(self.root)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(plan.blockers)
        changed = {change.path for change in plan.changes}
        self.assertIn('harnest.lock', changed)
        self.assertIn('acme-agent.yaml', changed)
        backup = apply_project_plan(plan)
        self.assertEqual((backup / 'source/acme-agent.yaml').read_bytes(), before['acme-agent.yaml'].content)
        self.assertEqual(yaml.safe_load((self.root / 'acme-agent.yaml').read_text()),
                         {'team': 'custom-team', 'environment': 'development', 'custom': 'kept'})
        self.assertEqual(self.planner().plan_upgrade(self.root).changes, ())

    def test_local_generated_file_edits_block_entire_upgrade(self):
        self.initialize()
        (self.root / '.github/workflows/agent.yml').write_text('user changes')
        before = snapshot(self.root)
        plan = self.planner().plan_upgrade(self.root)
        self.assertIn('local modifications', str(plan.blockers))
        with self.assertRaises(ProjectError):
            apply_project_plan(plan)
        self.assertEqual(snapshot(self.root), before)

    def test_stale_plan_rejects_changed_unrelated_source(self):
        self.initialize()
        plan = self.planner().plan_upgrade(self.root)
        (self.root / 'instructions.md').write_text('edited after preview')
        with self.assertRaisesRegex(ProjectError, 'changed since planning'):
            apply_project_plan(plan)
        self.assertIn('owner:', (self.root / 'acme-agent.yaml').read_text())

    def test_stale_plan_rejects_added_file(self):
        self.initialize()
        plan = self.planner().plan_upgrade(self.root)
        (self.root / 'new.txt').write_text('new')
        with self.assertRaises(ProjectError):
            apply_project_plan(plan)

    def test_missing_pack_and_migration_and_downgrade_are_blockers(self):
        self.initialize()
        self.assertIn('missing installed', str(self.planner(packs=[]).plan_upgrade(self.root).blockers))
        missing = ProjectPack('acme', 3)
        self.assertIn('missing migrations', str(self.planner(packs=[missing]).plan_upgrade(self.root).blockers))
        apply_project_plan(self.planner().plan_upgrade(self.root))
        self.assertIn('newer', str(self.planner(1).plan_upgrade(self.root).blockers))

    def test_new_pack_is_not_silently_adopted_during_upgrade(self):
        self.initialize()
        packs = [example.create_pack(1), ProjectPack('new-pack', 1)]
        self.assertIn('explicit adoption', str(self.planner(packs=packs).plan_upgrade(self.root).blockers))

    def test_multiple_packs_detect_conflicts_before_apply(self):
        other = ProjectPack('other', 1)
        other.initialize(lambda ctx: ChangePlan(ctx.yaml.set('acme-agent.yaml', key=('team',), value='other')))
        plan = self.planner(packs=[example.create_pack(), other]).plan_init(self.root, options={'acme': {'team': 'support'}})
        self.assertIn('overlapping', str(plan.blockers))
        self.assertFalse(self.root.exists())

    def test_pack_migrations_see_previous_migration_results(self):
        pack = ProjectPack('acme', 3)
        pack.migration(from_version=1, to_version=2)(lambda ctx: ChangePlan(ctx.yaml.rename_key('acme-agent.yaml', source=('owner',), destination=('team',))))
        def step_three(ctx):
            self.assertEqual(ctx.yaml.read('acme-agent.yaml')['team'], 'support')
            return ChangePlan(ctx.yaml.set('acme-agent.yaml', key=('migrated',), value=True))
        pack.migration(from_version=2, to_version=3)(step_three)
        self.initialize()
        plan = self.planner(packs=[pack]).plan_upgrade(self.root)
        self.assertFalse(plan.blockers)
        apply_project_plan(plan)
        self.assertTrue(yaml.safe_load((self.root / 'acme-agent.yaml').read_text())['migrated'])

    def test_rollback_restores_all_files_after_write_failure(self):
        self.initialize()
        before = snapshot(self.root)
        plan = self.planner().plan_upgrade(self.root)
        failed = False
        def fail_once(path, value):
            nonlocal failed
            if path == plan.root / 'harnest-packs.lock' and not failed:
                failed = True
                raise OSError('injected write failure')
            atomic_write(path, value)
        with patch('harnest.authoring.filesystem.atomic_write', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'injected'):
                apply_project_plan(plan)
        self.assertTrue(failed)
        self.assertEqual(snapshot(self.root), before)

    def test_backup_symlink_cannot_redirect_writes(self):
        if os.name == 'nt':
            self.skipTest('symlink creation requires privileges on Windows')
        self.initialize()
        plan = self.planner().plan_upgrade(self.root)
        shutil.rmtree(self.root / '.harnest')
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (self.root / '.harnest').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ProjectError, 'backup directory'):
            apply_project_plan(plan)
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_symlink_is_rejected(self):
        if os.name == 'nt':
            self.skipTest('symlink creation requires privileges on Windows')
        self.initialize()
        (self.root / 'linked').symlink_to(self.root / 'agent.py')
        with self.assertRaisesRegex(ProjectError, 'regular file'):
            self.planner().plan_upgrade(self.root)

    def test_cli_runs_real_company_init_preview_and_upgrade(self):
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'HARNEST_CLI': str(self.binary), 'ACME_DEMO_PACK_VERSION': '1'}
        result = subprocess.run([sys.executable, str(EXAMPLE), 'init', str(self.root), '--team', 'platform', '--minimal'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        env['ACME_DEMO_PACK_VERSION'] = '2'
        preview = subprocess.run([sys.executable, str(EXAMPLE), 'upgrade', str(self.root), '--json'], env=env, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)['changes'])
        self.assertIn('owner:', (self.root / 'acme-agent.yaml').read_text())
        applied = subprocess.run([sys.executable, str(EXAMPLE), 'upgrade', str(self.root), '--apply'], env=env, capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('team: platform', (self.root / 'acme-agent.yaml').read_text())

    def test_cli_supports_custom_commands_and_namespaced_options(self):
        class OtherOptions(BaseModel):
            team: str
        other = ProjectPack('other', 1, options=OtherOptions)
        cli = ProjectCLI('company', [example.create_pack(), other], harnest_command=(str(self.binary),))
        parser = cli.add_command('hello', lambda args: 7)
        parser.add_argument('--name')
        self.assertEqual(cli.run(['hello', '--name', 'user']), 7)
        output = io.StringIO()
        status = cli.run(['init', str(self.root), '--acme-team', 'support', '--other-team', 'infra', '--dry-run', '--json'], stdout=output)
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output.getvalue())['blockers'])
        self.assertFalse(self.root.exists())

    def test_failed_hook_is_a_blocked_plan_and_does_not_leak_its_error_payload(self):
        pack = ProjectPack('failure', 1)
        def fail(ctx):
            raise RuntimeError('secret-input-value')
        pack.initialize(fail)
        plan = self.planner(packs=[pack]).plan_init(self.root)
        self.assertIn('hook failed (RuntimeError)', str(plan.blockers))
        self.assertNotIn('secret-input-value', plan.render())
        self.assertFalse(self.root.exists())

    def test_existing_apply_lock_blocks_second_writer(self):
        self.initialize()
        before = snapshot(self.root)
        plan = self.planner().plan_upgrade(self.root)
        (self.root / '.harnest/project-apply.lock').touch()
        with self.assertRaisesRegex(ProjectError, 'another project apply'):
            apply_project_plan(plan)
        self.assertEqual(snapshot(self.root), before)

    def test_core_edit_conflicts_with_pack_owned_file(self):
        pack = example.create_pack(1)
        initializer = pack._initializer
        def initialize(ctx):
            original = initializer(ctx)
            return ChangePlan(*original.operations, ctx.files.write_text('legacy.py', 'from harnest import Agent\n'))
        pack._initializer = initialize
        plan = self.planner(packs=[pack]).plan_init(self.root, options={'acme': {'team': 'support'}})
        self.assertFalse(plan.blockers)
        apply_project_plan(plan)
        plan = self.planner(packs=[pack]).plan_upgrade(self.root)
        self.assertIn('overlapping', str(plan.blockers))

    def test_migrated_agent_compiles_for_both_backends(self):
        """Pack-generated agents compile without their authoring configuration or package."""
        from harnest.bundle import compile_artifact
        self.initialize()
        apply_project_plan(self.planner().plan_upgrade(self.root))
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'http://localhost:11434/v1', 'OPENAI_MODEL': 'test-model'}):
            for framework in ('adk', 'langgraph'):
                target = self.root.parent / f'compiled-{framework}'
                compile_artifact(self.root, target, framework=framework)
                self.assertTrue((target / 'harnest-agent').exists())
                report = json.loads((target / 'harnest-build-report.json').read_text())
                self.assertNotIn('resources', report['selection'])
                self.assertFalse((target / 'source/acme-agent.yaml').exists())

    def test_failed_init_rolls_back_new_project_directory(self):
        plan = self.planner().plan_init(self.root, options={'acme': {'team': 'support'}})
        def fail_source(path, value):
            if path == plan.root / 'agent.py':
                raise OSError('injected init failure')
            atomic_write(path, value)
        with patch('harnest.authoring.filesystem.atomic_write', side_effect=fail_source):
            with self.assertRaisesRegex(OSError, 'injected init'):
                apply_project_plan(plan)
        self.assertFalse(self.root.exists())

    def test_invalid_migrated_config_blocks_all_changes(self):
        self.initialize()
        (self.root / 'acme-agent.yaml').write_text('owner: ""\nenvironment: development\n')
        before = snapshot(self.root)
        plan = self.planner().plan_upgrade(self.root)
        self.assertIn('validation failed', str(plan.blockers))
        self.assertEqual(snapshot(self.root), before)

    def test_init_never_overwrites_existing_project(self):
        self.initialize()
        before = snapshot(self.root)
        with self.assertRaisesRegex(ProjectError, 'empty directory'):
            self.planner().plan_init(self.root)
        self.assertEqual(snapshot(self.root), before)

    def test_new_file_cannot_replace_an_existing_empty_directory(self):
        self.initialize()
        (self.root / 'user-dir').mkdir()
        pack = ProjectPack('acme', 2)
        pack.migration(from_version=1, to_version=2)(lambda ctx: ChangePlan(ctx.files.write_text('user-dir', 'file')))
        plan = self.planner(packs=[pack]).plan_upgrade(self.root)
        before = snapshot(self.root)
        with self.assertRaisesRegex(ProjectError, 'existing directory'):
            apply_project_plan(plan)
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue((self.root / 'user-dir').is_dir())

    def test_optional_string_cli_option_accepts_plain_text(self):
        class Options(BaseModel):
            region: str | None = None
        pack = ProjectPack('optional', 1, options=Options)
        pack.initialize(lambda ctx: ChangePlan(ctx.yaml.set('company.yaml', key=('region',), value=ctx.options.region)))
        cli = ProjectCLI('company', [pack], harnest_command=(str(self.binary),))
        status = cli.run(['init', str(self.root), '--region', 'london'], stdout=io.StringIO())
        self.assertEqual(status, 0)
        self.assertEqual(yaml.safe_load((self.root / 'company.yaml').read_text()), {'region': 'london'})
