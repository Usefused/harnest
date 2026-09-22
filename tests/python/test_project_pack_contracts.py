"""Validate project pack declarations and ownership rules without invoking a CLI."""
import unittest

from harnest.authoring import ChangePlan, ProjectError, ProjectPack, WritePolicy
from harnest.authoring.context import ProjectFiles, ProjectYAML, relative_path
from harnest.authoring.contracts import _File
from harnest.authoring.operations import OperationEngine, read_lock


class ProjectPackContractTests(unittest.TestCase):
    def test_names_and_versions_are_validated(self):
        for name, version in [('harnest', 1), ('../acme', 1), ('ok', True), ('ok', 0)]:
            with self.subTest(name=name, version=version), self.assertRaises(ProjectError):
                ProjectPack(name, version)

    def test_registration_rejects_duplicate_and_skipped_migrations(self):
        pack = ProjectPack('acme', 3)
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

    def test_lock_rejects_malformed_ownership(self):
        for value in [b'{}', b'[]', b'{"version":1,"packs":{},"claims":[{}]}']:
            with self.subTest(value=value), self.assertRaises(ProjectError):
                read_lock({'harnest-packs.lock': _File(value)})
