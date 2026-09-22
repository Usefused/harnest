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

from harnest.authoring import ChangePlan, ProjectCLI, ProjectError, ProjectPack, ProjectPlanner, apply_project_plan
from harnest.authoring.filesystem import atomic_write, snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples/project-packs/acme.py'
spec = importlib.util.spec_from_file_location('acme_pack_example', EXAMPLE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


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
        from harnest.bundle import compile_artifact
        self.initialize()
        apply_project_plan(self.planner().plan_upgrade(self.root))
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'http://localhost:11434/v1', 'OPENAI_MODEL': 'test-model'}):
            for framework in ('adk', 'langgraph'):
                target = self.root.parent / f'compiled-{framework}'
                compile_artifact(self.root, target, framework=framework)
                self.assertTrue((target / 'harnest-agent').exists())

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
