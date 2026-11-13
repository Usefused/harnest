"""Validate pack declarations, pack.yaml loading and ownership rules without invoking a CLI."""
from pathlib import Path
import tempfile
import textwrap
import unittest

from harnest.authoring import ChangePlan, ProjectError, ProjectPack, ProjectPlanner, WritePolicy, load_pack
from harnest.authoring.packs import parse_options
from harnest.authoring.context import ProjectFiles, ProjectYAML, relative_path
from harnest.authoring.contracts import _File
from harnest.authoring.operations import OperationEngine, read_lock


class ProjectPackContractTests(unittest.TestCase):
    def test_names_and_versions_are_validated(self):
        for version in [True, 0, '1']:
            with self.subTest(version=version), self.assertRaises(ProjectError):
                ProjectPack(version)
        for name in ['harnest', '../acme', 'Bad', '']:
            with self.subTest(name=name), self.assertRaises(ProjectError):
                ProjectPack(1).bind(name, Path('.'))

    def test_unbound_packs_cannot_be_planned(self):
        with self.assertRaisesRegex(ProjectError, 'pack directory'):
            ProjectPlanner([ProjectPack(1)])

    def test_commands_cannot_shadow_harnest_or_repeat(self):
        pack = ProjectPack(1)
        pack.command('whoami', help='who owns this agent')(lambda args: 0)
        self.assertEqual([(c.name, c.help) for c in pack.commands], [('whoami', 'who owns this agent')])
        for name in ['whoami', 'init', 'compile', 'Bad', 'bad_name']:
            with self.subTest(name=name), self.assertRaises(ProjectError):
                pack.command(name)

    def test_registration_rejects_duplicate_and_skipped_migrations(self):
        pack = ProjectPack(3)
        pack.initialize(lambda ctx: ChangePlan())
        with self.assertRaises(ProjectError):
            pack.initialize(lambda ctx: ChangePlan())
        with self.assertRaises(ProjectError):
            pack.migration(from_version=1, to_version=3)
        pack.migration(from_version=1, to_version=2)(lambda ctx: ChangePlan())
        with self.assertRaises(ProjectError):
            pack.migration(from_version=1, to_version=2)

    def test_paths_cannot_escape_or_modify_reserved_state(self):
        for path in ['../outside', '/absolute', 'a/../b', 'a//b', '.git/config', '.harnest/x', 'harnest-packs.lock', 'a\\b', '.']:
            with self.subTest(path=path), self.assertRaises(ProjectError):
                relative_path(path)

    def test_static_policy_requires_enum(self):
        with self.assertRaises(ProjectError):
            ProjectFiles({}, None).write_text('a', 'x', policy='managed')

    def test_change_plan_rejects_arbitrary_values(self):
        with self.assertRaises(ProjectError):
            ChangePlan({'path': 'anything'})

    def test_yaml_read_rejects_duplicate_keys_and_multiple_documents(self):
        for text in ['a: 1\na: 2\n', 'a: 1\n---\nb: 2\n', '- one\n']:
            with self.subTest(text=text), self.assertRaises(ProjectError):
                ProjectYAML({'c.yaml': _File(text.encode())}).read('c.yaml')

    def test_distinct_pack_keys_can_share_a_configuration_file(self):
        engine = OperationEngine({}, read_lock({}))
        yaml = ProjectYAML(engine.files)
        engine.apply('one', yaml.set('c.yaml', key=('one',), value=1))
        engine.apply('two', yaml.set('c.yaml', key=('two',), value=2))
        self.assertEqual(yaml.read('c.yaml'), {'one': 1, 'two': 2})

    def test_overlapping_pack_keys_fail_even_for_identical_values(self):
        engine = OperationEngine({}, read_lock({}))
        yaml = ProjectYAML(engine.files)
        engine.apply('one', yaml.set('c.yaml', key=('settings',), value={'a': 1}))
        with self.assertRaisesRegex(ProjectError, 'overlapping'):
            engine.apply('two', yaml.set('c.yaml', key=('settings', 'a'), value=1))

    def test_whole_file_and_yaml_claims_conflict(self):
        engine = OperationEngine({}, read_lock({}))
        engine.apply('one', ProjectFiles({}, None).write_text('c.yaml', 'a: 1\n'))
        with self.assertRaisesRegex(ProjectError, 'overlapping'):
            engine.apply('two', ProjectYAML(engine.files).set('c.yaml', key=('b',), value=2))

    def test_managed_updates_preserve_user_modifications(self):
        engine = OperationEngine({}, read_lock({}))
        files = ProjectFiles(engine.files, None)
        engine.apply('one', files.write_text('ci.txt', 'original'))
        engine.files['ci.txt'] = _File(b'edited by user')
        with self.assertRaisesRegex(ProjectError, 'local modifications'):
            engine.apply('one', files.write_text('ci.txt', 'new version', policy=WritePolicy.MANAGED))
        self.assertEqual(engine.files['ci.txt'].content, b'edited by user')

    def test_if_missing_does_not_claim_existing_user_files(self):
        engine = OperationEngine({'ci.txt': _File(b'user')}, read_lock({}))
        engine.apply('one', ProjectFiles(engine.files, None).write_text('ci.txt', 'default'))
        self.assertEqual(engine.files['ci.txt'].content, b'user')
        self.assertEqual(engine.lock['claims'], [])

    def test_managed_yaml_updates_only_unchanged_owned_key(self):
        engine = OperationEngine({}, read_lock({}))
        yaml = ProjectYAML(engine.files)
        engine.apply('one', yaml.set('c.yaml', key=('a',), value=1))
        engine.files['c.yaml'] = _File(b'a: 1\nuser: kept\n')
        engine.apply('one', yaml.set('c.yaml', key=('a',), value=2, policy=WritePolicy.MANAGED))
        self.assertEqual(yaml.read('c.yaml'), {'a': 2, 'user': 'kept'})
        engine.files['c.yaml'] = _File(b'a: 3\nuser: kept\n')
        with self.assertRaises(ProjectError):
            engine.apply('one', yaml.set('c.yaml', key=('a',), value=4, policy=WritePolicy.MANAGED))

    def test_rename_carries_user_value_and_preserves_other_keys(self):
        engine = OperationEngine({'c.yaml': _File(b'owner: user-choice\nextra: 4\n')}, read_lock({}))
        yaml = ProjectYAML(engine.files)
        op = yaml.rename_key('c.yaml', source=('owner',), destination=('team',))
        engine.apply('one', op)
        engine.apply('one', op)
        self.assertEqual(yaml.read('c.yaml'), {'extra': 4, 'team': 'user-choice'})

    def test_rename_refuses_existing_destination(self):
        engine = OperationEngine({'c.yaml': _File(b'owner: one\nteam: two\n')}, read_lock({}))
        with self.assertRaisesRegex(ProjectError, 'destination already exists'):
            engine.apply('one', ProjectYAML(engine.files).rename_key('c.yaml', source=('owner',), destination=('team',)))

    def test_delete_requires_unchanged_ownership(self):
        engine = OperationEngine({}, read_lock({}))
        files = ProjectFiles(engine.files, None)
        engine.apply('one', files.write_text('owned', 'value'))
        engine.apply('one', files.delete('owned'))
        self.assertNotIn('owned', engine.files)
        engine.files['unowned'] = _File(b'user')
        with self.assertRaises(ProjectError):
            engine.apply('one', files.delete('unowned'))

    def test_version_one_lock_asks_for_upgrade(self):
        with self.assertRaisesRegex(ProjectError, 'harnest upgrade'):
            read_lock({'harnest-packs.lock': _File(b'{"version":1,"packs":{},"claims":[]}')})
        self.assertEqual(read_lock({})['version'], 2)

    def test_lock_rejects_malformed_ownership(self):
        for value in [b'{}', b'[]', b'{"version":2,"packs":{},"claims":[{}],"resources":{}}',
                      b'{"version":2,"packs":{},"claims":[],"resources":[]}',
                      b'{"version":2,"packs":{},"claims":[]}']:
            with self.subTest(value=value), self.assertRaises(ProjectError):
                read_lock({'harnest-packs.lock': _File(value)})


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(textwrap.dedent(text))
    return root


MANIFEST = "apiVersion: harnest.dev/pack/v1\nname: acme\nversion: '1.0'\ntitle: Acme\n"


class PackLoaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_pack_yaml_names_the_project_hooks_and_resolves_templates(self):
        pack = load_pack(_write(self.root / 'acme', {
            'pack.yaml': MANIFEST,
            'pack.py': """
                from harnest.authoring import ProjectPack
                from helpers import VERSION
                pack = ProjectPack(VERSION, templates='templates')
            """,
            'helpers.py': 'VERSION = 3\n',
        }))
        self.assertEqual((pack.name, pack.manifest.version, pack.manifest.command_name), ('acme', '1.0', 'acme'))
        self.assertEqual((pack.project.name, pack.project.schema_version), ('acme', 3))
        self.assertEqual(pack.project.templates, pack.root / 'templates')

    def test_manifest_alone_is_a_studio_or_identity_only_pack(self):
        pack = load_pack(_write(self.root / 'ui', {'pack.yaml': MANIFEST + 'studio: {resources: []}\n'}))
        self.assertIsNone(pack.project)
        self.assertEqual(pack.studio, {'resources': []})

    def test_invalid_manifests_and_modules_are_rejected(self):
        cases = {
            'unknown key': (MANIFEST + 'surprise: 1\n', None),
            'old api': (MANIFEST.replace('pack/v1', 'studio-pack/v1'), None),
            'bad version': (MANIFEST.replace("'1.0'", 'not-a-version'), None),
            'bad command': (MANIFEST + 'command: Bad\n', None),
            'future harnest': (MANIFEST + "requires: {harnest: '>=999'}\n", None),
            'no pack object': (MANIFEST, 'value = 1\n'),
            'import error': (MANIFEST, 'raise RuntimeError("boom")\n'),
        }
        for label, (manifest, module) in cases.items():
            with self.subTest(label):
                files = {'pack.yaml': manifest, **({'pack.py': module} if module else {})}
                with self.assertRaises(ProjectError):
                    load_pack(_write(self.root / label.replace(' ', '-'), files))

    def test_removed_layouts_explain_the_migration(self):
        legacy = _write(self.root / 'legacy-studio', {'studio-pack.yaml': 'name: acme\n'})
        with self.assertRaisesRegex(ProjectError, 'studio-pack.yaml is no longer supported'):
            load_pack(legacy)
        cli = _write(self.root / 'legacy-cli', {'pack.py': 'pack = None\n'})
        with self.assertRaisesRegex(ProjectError, 'pack.yaml is required'):
            load_pack(cli)

    def test_pack_yaml_is_read_without_running_pack_py(self):
        pack = load_pack(_write(self.root / 'inert', {'pack.yaml': MANIFEST, 'pack.py': 'raise SystemExit(9)\n'}),
                         import_project=False)
        self.assertIsNone(pack.project)

    def test_options_target_one_pack_or_need_a_prefix(self):
        one = load_pack(_write(self.root / 'one', {'pack.yaml': MANIFEST}))
        self.assertEqual(parse_options(['team=support', 'tags=["a"]', 'max-items=3'], [one]),
                         {'acme': {'team': 'support', 'tags': ['a'], 'max_items': '3'}})
        other = load_pack(_write(self.root / 'two', {'pack.yaml': MANIFEST.replace('acme', 'beta')}))
        self.assertEqual(parse_options(['beta.team=x'], [one, other]), {'beta': {'team': 'x'}})
        for values in (['team=x'], ['gamma.team=x'], ['novalue'], ['tags=[oops']):
            with self.subTest(values=values), self.assertRaises(ProjectError):
                parse_options(values, [one, other] if values == ['team=x'] else [one])
