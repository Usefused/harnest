"""Company Studio launch, real CLI initialization and delegated process contracts."""
import io
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import yaml

from harnest.authoring import ProjectCLI, ProjectError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'studio/src'))

from harnest_builder.app import create_app
from harnest_builder.__main__ import main as studio_main

UI = ROOT / 'examples/company-cli/src/acme_cli/studio'


class CompanyStudioTests(unittest.TestCase):
    """Exercise the same argv through HTTP jobs and a separately running company CLI."""

    @classmethod
    def setUpClass(cls):
        """Compile the real core CLI once for the process integration boundary."""
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / 'harnest'
        subprocess.run(['go', 'build', '-o', str(cls.binary), './cmd/harnest'], cwd=ROOT, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        """Release the temporary native executable after all subprocesses have finished."""
        cls.build.cleanup()

    def setUp(self):
        """Use a script path with spaces to catch accidental shell/string command handling."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        script = self.root / 'company cli.py'
        script.write_text('''from harnest.authoring import ProjectCLI, ProjectPack, ChangePlan
from pydantic import BaseModel
class Options(BaseModel):
    team: str
pack = ProjectPack('acme', 1, options=Options)
@pack.initialize
def initialize(context):
    return ChangePlan(context.files.write_text('docs/team.md', context.options.team))
cli = ProjectCLI('acme', [pack], harnest_command=CORE)
cli.add_studio(command=COMMAND, packs=[UI], init_args=['--team', 'support team'])
raise SystemExit(cli.run())
'''.replace('CORE', repr((str(self.binary),))).replace('COMMAND', repr((sys.executable, str(script)))).replace('UI', repr(str(UI))))
        self.command = (sys.executable, str(script))
        # Child processes must resolve source packages independently of their workspace cwd.
        self.env = self.enterContext(patch.dict(os.environ, {'PYTHONPATH': os.pathsep.join((str(ROOT / 'src'), str(ROOT / 'studio/src')))}))

    def wait(self, app, identity):
        """Wait for a terminal job state with a bounded deadline."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = next(item for item in app.state.jobs.list() if item['id'] == identity)
            if job['status'] != 'running':
                return job
            time.sleep(.02)
        self.fail('Company command did not complete')

    def test_http_init_runs_company_hooks_and_core_commands(self):
        """Studio choices and required team defaults survive the entire subprocess path."""
        app = create_app(self.root, self.command, token='test', init_args=('--team', 'support team'))
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
            client.headers['Authorization'] = 'Bearer test'
            self.assertEqual(client.get('/api/workspace').json()['cli'], {'command': list(self.command), 'init_args': ['--team', 'support team']})
            response = client.post('/api/command', json={'action': 'init', 'name': 'company-agent', 'framework': 'adk', 'mode': 'managed', 'profile': 'example'})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait(app, response.json()['id'])
            self.assertEqual(job['status'], 'succeeded', job['output'])
            self.assertEqual(job['argv'][:2], list(self.command))
            agent = self.root / 'company-agent'
            self.assertEqual((agent / 'docs/team.md').read_text(), 'support team')
            self.assertEqual(json.loads((agent / 'harnest-packs.lock').read_text())['packs'], {'acme': 1})
            self.assertEqual(yaml.safe_load((agent / 'config.yaml').read_text())['spec']['framework']['mode'], 'managed')
            metrics = client.get('/api/evaluation-metrics')
            self.assertEqual(metrics.status_code, 200, metrics.text)
            response = client.post('/api/command', json={'action': 'add', 'project': 'company-agent', 'kind': 'tool', 'name': 'greet'})
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait(app, response.json()['id'])
            self.assertEqual(job['status'], 'succeeded', job['output'])
            self.assertNotIn('--team', job['args'])
            self.assertTrue((agent / 'tools/greet.py').is_file())

    def test_advanced_init_and_incompatible_flags(self):
        """Company CLI preserves advanced scaffolds and rejects conflicting profiles before writes."""
        agent = self.root / 'advanced-agent'
        process = subprocess.run([*self.command, 'init', str(agent), '--mode', 'advanced', '--minimal', '--team', 'support'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(yaml.safe_load((agent / 'config.yaml').read_text())['spec']['framework']['mode'], 'advanced')
        for flags in (['--example', '--minimal'], ['--template', 'sales', '--mode', 'managed'], ['--template', 'sales', '--example']):
            destination = self.root / 'invalid-agent'
            process = subprocess.run([*self.command, 'init', str(destination), *flags, '--team', 'support'], capture_output=True, text=True)
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertFalse(destination.exists())

    def test_launch_binds_assets_and_safe_ui_keeps_company_cli(self):
        """Safe UI removes external presentation without bypassing company init policy."""
        for safe in (False, True):
            with self.subTest(safe=safe), patch('harnest_builder.__main__.uvicorn.run') as server:
                studio_main(arguments=['--workspace', str(self.root), *(['--safe-ui'] if safe else [])],
                            embedded_packs=[UI], cli_command=self.command, init_args=['--team', 'support team'])
                app = server.call_args.args[0]
                with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
                    client.headers['Authorization'] = 'Bearer ' + app.state.token
                    catalog = client.get('/api/ui').json()
                    welcome = next(item for item in catalog['contributions'] if item['slot'] == 'welcome')
                    self.assertEqual(welcome['id'], 'fused-studio/welcome' if safe else 'acme-workspace/welcome')
                    self.assertEqual(client.get('/api/workspace').json()['cli']['command'], list(self.command))

    def test_cli_dispatch_help_exit_codes_and_custom_precedence(self):
        """Forwarded commands keep core argument parsing while explicit company handlers win."""
        cli = ProjectCLI('acme', [], harnest_command=(str(self.binary),))
        cli.add_command('compile', lambda args: 17)
        cli.add_studio(command=self.command, packs=[UI])
        self.assertEqual(cli.run(['compile']), 17)
        with patch('harnest_builder.__main__.uvicorn.run') as server:
            self.assertEqual(cli.run(['studio', '--workspace', str(self.root)]), 0)
            app = server.call_args.args[0]
            with TestClient(app):
                self.assertEqual(app.state.jobs.command, self.command)
        process = subprocess.run([*self.command, 'compile', '--help'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        process = subprocess.run([*self.command, 'compile', '--not-a-real-flag'], capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        original_import = builtins.__import__

        def without_studio(name, *args, **kwargs):
            """Simulate the optional web package being absent, preserving all other imports."""
            if name == 'harnest_builder.__main__':
                raise ModuleNotFoundError('Studio missing', name='harnest_builder')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=without_studio):
            errors = io.StringIO()
            self.assertEqual(cli.run(['studio'], stderr=errors), 1)
            self.assertIn('Studio is not installed', errors.getvalue())
        with self.assertRaises(ProjectError):
            cli.add_studio(command=self.command)
