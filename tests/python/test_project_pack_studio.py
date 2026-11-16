"""Packs in Studio and the branded command: real CLI initialization and delegated processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from harnest.authoring.distribution import build_pack

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'studio/src'))

from harnest_builder.app import create_app
from harnest_builder.__main__ import main as studio_main

WELCOME = ROOT / 'examples/packs/acme/assets/welcome.html'

PACK_YAML = '''apiVersion: harnest.dev/pack/v1
name: acme
version: 1.0.0
title: Acme agents
command: acme
studio:
  ui:
    apiVersion: harnest.dev/studio-ui/v1
    assets: [assets/welcome.html]
    contributions:
      - {id: welcome, slot: welcome, mode: frame, entry: assets/welcome.html, replaces: fused-studio/welcome, title: Acme}
'''

PACK_PY = '''from pydantic import BaseModel
from harnest.authoring import ChangePlan, ProjectPack

class Options(BaseModel):
    team: str

pack = ProjectPack(1, options=Options)

@pack.initialize
def initialize(context):
    return ChangePlan(context.files.write_text('docs/team.md', context.options.team))

@pack.command('hello', help='exit with a known code')
def hello(args):
    return 17
'''


class PackStudioTests(unittest.TestCase):
    """Exercise one pack through Studio jobs, the Studio launcher and its built command."""

    @classmethod
    def setUpClass(cls):
        """Compile the real core CLI once for the process integration boundary."""
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / 'harnest'
        subprocess.run(['go', 'build', '-o', str(cls.binary), './cmd/harnest'], cwd=ROOT, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        """Use a pack path with spaces to catch accidental shell/string command handling."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pack = self.root / 'acme pack'
        (self.pack / 'assets').mkdir(parents=True)
        (self.pack / 'pack.yaml').write_text(PACK_YAML)
        (self.pack / 'pack.py').write_text(PACK_PY)
        shutil.copy(WELCOME, self.pack / 'assets/welcome.html')
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.command = (str(self.binary),)
        # Child processes must resolve source packages independently of their workspace cwd.
        self.enterContext(patch.dict(os.environ, {
            'PYTHONPATH': os.pathsep.join((str(ROOT / 'src'), str(ROOT / 'studio/src'))),
            'HARNEST_PYTHON': sys.executable,
        }))

    def wait(self, app, identity):
        """Wait for a terminal job state with a bounded deadline."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = next(item for item in app.state.jobs.list() if item['id'] == identity)
            if job['status'] != 'running':
                return job
            time.sleep(.02)
        self.fail('Pack command did not complete')

    def test_http_init_runs_pack_hooks_and_core_commands(self):
        """Studio choices and required pack options survive the entire subprocess path."""
        init_args = ('--pack', str(self.pack), '--option', 'team=support team')
        app = create_app(self.workspace, self.command, token='test', init_args=init_args)
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
            client.headers['Authorization'] = 'Bearer test'
            self.assertEqual(client.get('/api/workspace').json()['cli'], {'command': list(self.command), 'init_args': list(init_args)})
            response = client.post('/api/command', json={'action': 'init', 'name': 'company-agent', 'framework': 'adk', 'mode': 'managed', 'profile': 'example'})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait(app, response.json()['id'])
            self.assertEqual(job['status'], 'succeeded', job['output'])
            agent = self.workspace / 'company-agent'
            self.assertEqual((agent / 'docs/team.md').read_text(), 'support team')
            self.assertEqual(json.loads((agent / 'harnest-packs.lock').read_text())['packs'], {'acme': 1})
            self.assertEqual(yaml.safe_load((agent / 'config.yaml').read_text())['spec']['framework']['mode'], 'managed')
            response = client.post('/api/command', json={'action': 'add', 'project': 'company-agent', 'kind': 'tool', 'name': 'greet'})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait(app, response.json()['id'])
            self.assertEqual(job['status'], 'succeeded', job['output'])
            self.assertNotIn('--pack', job['args'])
            self.assertTrue((agent / 'tools/greet.py').is_file())

    def test_advanced_init_and_incompatible_flags(self):
        """`init --pack` keeps advanced scaffolds and rejects conflicting profiles before writes."""
        agent = self.root / 'advanced-agent'
        process = subprocess.run([*self.command, 'init', str(agent), '--pack', str(self.pack), '--mode', 'advanced', '--minimal', '--option', 'team=support'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(yaml.safe_load((agent / 'config.yaml').read_text())['spec']['framework']['mode'], 'advanced')
        for flags in (['--example', '--minimal'], ['--template', 'sales', '--mode', 'managed'], ['--template', 'sales', '--example']):
            destination = self.root / 'invalid-agent'
            process = subprocess.run([*self.command, 'init', str(destination), '--pack', str(self.pack), *flags, '--option', 'team=support'], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0, process.stderr)
            self.assertFalse(destination.exists())

    def test_launch_binds_pack_ui_and_pack_hooks(self):
        """One --pack supplies Studio UI and the init hooks for agents Studio creates."""
        with patch('harnest_builder.__main__.uvicorn.run') as server:
            studio_main(arguments=['--workspace', str(self.workspace), '--cli', str(self.binary), '--pack', str(self.pack),
                                   '--option', 'team=support'])
            app = server.call_args.args[0]
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
                client.headers['Authorization'] = 'Bearer ' + app.state.token
                catalog = client.get('/api/ui').json()
                welcome = next(item for item in catalog['contributions'] if item['slot'] == 'welcome')
                self.assertEqual(welcome['id'], 'acme/welcome')
                # The browser's "Restore default UI" link still recovers the bundled UI.
                safe = client.get('/api/ui?safe=true').json()
                self.assertEqual(next(item for item in safe['contributions'] if item['slot'] == 'welcome')['id'], 'fused-studio/welcome')
                cli = client.get('/api/workspace').json()['cli']
                self.assertEqual(cli['command'], list(self.command))
                self.assertEqual(cli['init_args'], ['--pack', str(self.pack.resolve()), '--option', 'team=support'])
        with self.assertRaises(SystemExit):
            studio_main(arguments=['--workspace', str(self.workspace), '--cli', str(self.binary), '--safe-ui'])

    def test_built_command_routes_pack_custom_and_core_commands(self):
        """The branded command attaches its pack, runs company commands and forwards the rest."""
        wheel = build_pack(self.pack, self.root / 'dist')
        target = self.root / 'installed'
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index', '--target', str(target), str(wheel)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = [sys.executable, '-m', 'harnest_pack_acme.launcher']
        env = {**os.environ, 'HARNEST_CLI': str(self.binary), 'PYTHONPATH': os.pathsep.join((str(target), os.environ['PYTHONPATH']))}
        usage = subprocess.run([*launcher, '--help'], env=env, capture_output=True, text=True)
        self.assertEqual(usage.returncode, 0)
        self.assertIn('hello', usage.stdout)
        self.assertEqual(subprocess.run([*launcher, 'hello'], env=env, capture_output=True).returncode, 17)
        agent = self.root / 'branded-agent'
        created = subprocess.run([*launcher, 'init', str(agent), '--minimal', '--option', 'team=billing'], env=env, capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual((agent / 'docs/team.md').read_text(), 'billing')
        self.assertEqual(subprocess.run([*launcher, 'compile', '--help'], env=env, capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run([*launcher, 'compile', '--not-a-real-flag'], env=env, capture_output=True).returncode, 0)
        missing = subprocess.run([*launcher, 'compile'], env={**env, 'HARNEST_CLI': str(self.root / 'absent')}, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 127)
        self.assertIn('HARNEST_CLI', missing.stderr)
