"""Studio OAuth, reviewed MCP provisioning, secret isolation, and AI discovery integration."""

import json
from pathlib import Path
import sys
import time
from collections import deque
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from _test_context import enter_context

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent-builder/src'))
from harnest_builder.app import create_app
from harnest_builder.mcp_credentials import CredentialStore
from harnest_builder.jobs import Jobs
from harnest.fused_connectors import FusedConnectorsService
from test_playground_connectors import FakeAuthClient, FakeAdminClient, _Server, _session


class ManagementClient(FakeAdminClient):
    """Retain the exact remote mutation count to detect unreviewed or repeated provisioning."""
    deployments = 0
    tokens = 0
    fail_token = False

    def deploy_server(self, config, owner_team=None):
        """Record a single approved deployment independently of subsequent token issuance."""
        type(self).deployments += 1
        type(self).last_owner = owner_team
        return super().deploy_server(config)

    def generate_token(self, reference, payload):
        """Return a recognizable secret that must never appear in responses or prompts."""
        type(self).tokens += 1
        if self.fail_token:
            raise OSError('upstream-secret')
        return SimpleNamespace(token='private-execution-token')


class StudioMCPTests(unittest.TestCase):
    """Use real HTTP routes and source transactions with a deterministic Admin transport."""

    def setUp(self):
        """Isolate credentials, workspace, source files, browser cookies, and remote state."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'workspace'
        self.project = self.root / 'agent'
        self.project.mkdir(parents=True)
        (self.project / 'config.yaml').write_text('metadata:\n  name: example\n')
        (self.project / 'agent.py').write_text('from harnest.agent import Agent\nroot_agent = Agent(name="example")\n')
        self.credentials = CredentialStore(base / 'credentials')
        self.connector = FusedConnectorsService(engine_url='https://engine.test')
        self.connector._sessions['browser'] = _session()
        self.app = create_app(self.root, '/missing/harnest', token='launch', connector=self.connector, credentials=self.credentials)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:1940', client=('127.0.0.1', 1234), headers={'Authorization': 'Bearer launch'})
        self.client.cookies.set('harnest-studio-fused-1940', 'browser')
        self.addCleanup(self.client.close)
        enter_context(self, patch('harnest.fused_connectors.FusedAdminClient', ManagementClient))
        ManagementClient.deployments = ManagementClient.tokens = 0
        ManagementClient.fail_token = False
        ManagementClient.servers = [_Server('existing', '1.0.0')]

    def plan(self, **overrides):
        """Prepare an explicit operation allowlist without mutating the fake Engine."""
        body = {'project': 'agent', 'kind': 'create', 'resource': 'fused', 'name': 'my-mcp',
                'services': [{'slug': 'verify-service-a', 'version': '1.0.27', 'operations': ['op-1']}]}
        return self.client.post('/api/mcp/plan', json={**body, **overrides})

    def test_review_apply_scopes_credentials_and_is_idempotent(self):
        """The approved plan creates once, writes real source, and never reveals token plaintext."""
        response = self.plan(owner_team='chosen-team', bucket='chosen-bucket')
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        self.assertEqual((ManagementClient.deployments, ManagementClient.tokens), (0, 0))
        self.assertFalse((self.project / 'mcp/fused.py').exists())
        self.assertEqual(review['connection']['configuration']['bucket'], 'chosen-bucket')
        for _ in range(2):
            applied = self.client.post('/api/mcp/apply', json={'review': review['mcp_review']})
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertNotIn('private-execution-token', applied.text)
        self.assertEqual((ManagementClient.deployments, ManagementClient.tokens), (1, 1))
        self.assertEqual(ManagementClient.last_owner, 'chosen-team')
        source = (self.project / 'mcp/fused.py').read_text()
        self.assertIn('MCPClient.streamable_http', source)
        self.assertNotIn('private-execution-token', source)
        credentials = self.credentials.read(self.project)
        self.assertIn('private-execution-token', credentials.values())
        self.assertEqual(self.credentials.read(self.root / 'other'), {})
        self.assertEqual(next(self.credentials.directory.glob('*.json')).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-execution-token', self.client.get('/api/project?project=agent').text)

    def test_stale_revision_prevents_all_remote_mutations(self):
        """External edits invalidate approval before server creation or credential issuance."""
        review = self.plan().json()
        (self.project / 'mcp').mkdir()
        (self.project / 'mcp/fused.py').write_text('# external change\n')
        response = self.client.post('/api/mcp/apply', json={'review': review['mcp_review']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ManagementClient.deployments, 0)

    def test_uncertain_token_failure_does_not_redeploy_on_retry(self):
        """A server may exist after token failure; retries must direct the user to discovery."""
        review = self.plan().json()
        ManagementClient.fail_token = True
        first = self.client.post('/api/mcp/apply', json={'review': review['mcp_review']})
        second = self.client.post('/api/mcp/apply', json={'review': review['mcp_review']})
        self.assertEqual((first.status_code, second.status_code), (502, 409))
        self.assertNotIn('upstream-secret', first.text)
        self.assertEqual(ManagementClient.deployments, 1)

    def test_local_write_failure_can_retry_without_another_token(self):
        """Retain a successful connection when source publication fails after remote success."""
        review = self.plan().json()
        with patch.object(self.app.state.mcp.workspace, 'apply', side_effect=OSError('disk')):
            self.assertEqual(self.client.post('/api/mcp/apply', json={'review': review['mcp_review']}).status_code, 502)
        self.assertEqual(self.client.post('/api/mcp/apply', json={'review': review['mcp_review']}).status_code, 200)
        self.assertEqual((ManagementClient.deployments, ManagementClient.tokens), (1, 1))

    def test_missing_scope_and_invented_operations_fail_before_review(self):
        """Never interpret an empty operation selection as authorization for the whole service."""
        for services in ([{'slug': 'verify-service-a', 'version': '1.0.27'}],
                         [{'slug': 'verify-service-a', 'version': '1.0.27', 'operations': ['invented']}],
                         [{'slug': 'invented', 'version': '1.0.27', 'select_all': True}]):
            self.assertEqual(self.plan(services=services).status_code, 422)
        self.assertEqual(ManagementClient.deployments, 0)

    def test_http_credentials_and_subagent_ownership(self):
        """Existing HTTP connections use the same preview and vault without Fused authentication."""
        owner = self.project / 'subagents/worker'
        owner.mkdir(parents=True)
        (owner / 'agent.py').write_text('pass\n')
        review = self.plan(kind='http', services=[], owner='subagents/worker/agent.py', url='https://mcp.test/mcp', bearer='manual-secret').json()
        self.assertNotIn('manual-secret', json.dumps(review))
        response = self.client.post('/api/mcp/apply', json={'review': review['mcp_review']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((owner / 'mcp/fused.py').exists())
        self.assertEqual(ManagementClient.tokens, 0)

    def test_deployment_bindings_preserve_unrelated_services(self):
        """Generated connections add secret references without changing service topology."""
        import yaml
        manifest = {"version": 1, "name": "sample", "backend": "local", "agents": {
            "main": {"image": "example:latest", "healthcheck": {"command": ["true"]},
                     "environment": {"EXISTING": "kept"}}}, "services": {}}
        (self.project / "harnest-deployment.yaml").write_text(yaml.safe_dump(manifest))
        response = self.plan(kind="http", url="https://example.test/mcp", bearer="deployment-secret")
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        after = yaml.safe_load(review["files"][1]["text"])
        environment = after["agents"]["main"]["environment"]
        self.assertEqual(environment["EXISTING"], "kept")
        for name in review["connection"]["credential_variables"]:
            self.assertEqual(environment[name], {"secret": name})
        self.assertNotIn("deployment-secret", json.dumps(review))

    def test_owner_change_and_symlink_storage_fail_closed(self):
        """Review ownership and credential paths remain bound to their original local objects."""
        review = self.plan().json()
        (self.project / "agent.py").write_text("# changed owner\n")
        self.assertEqual(self.client.post('/api/mcp/apply', json={'review': review['mcp_review']}).status_code, 409)
        self.credentials.directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.credentials.save(self.project, {"HARNEST_MCP_TEST_TOKEN": "secret"})
        self.assertEqual(ManagementClient.deployments, 0)

    def test_job_process_receives_only_its_project_credentials(self):
        """The actual child process receives bindings while its displayed output is redacted."""
        self.credentials.save(self.project, {"HARNEST_MCP_TEST_TOKEN": "process-secret"})
        jobs = Jobs(sys.executable, self.app.state.mcp.workspace, self.credentials)
        job = {"id": "test", "project": "agent", "status": "running", "serving": False,
               "argv": [sys.executable, "-c", "import os; print(os.environ['HARNEST_MCP_TEST_TOKEN'])"]}
        jobs.items[job["id"]] = job
        jobs._execute(job)
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["output"].strip(), "[redacted]")
        tail = deque([b"cret noise rest"], maxlen=1)
        self.assertNotIn("cret", Jobs._safe_output(tail, ["process-secret"], pending=False))

    def test_wrong_browser_expiry_and_refresh(self):
        """OAuth state is bound to the initiating browser and refresh grants stay server-side."""
        with patch('harnest.fused_connectors.FusedAuthClient', FakeAuthClient), patch.object(self.connector, '_register_client', return_value=('id', 'fos_temporary')):
            self.connector.authorize_url('http://localhost/callback', 'original')
            state = next(iter(self.connector._flows))
            with self.assertRaises(Exception):
                self.connector.complete_authorization('code', state, 'http://localhost/callback', 'different')
            self.assertFalse(self.connector.connected('different'))
            self.connector.authorize_url('http://localhost/callback', 'original')
            state = next(iter(self.connector._flows))
            flow = self.connector._flows[state]
            self.connector._flows[state] = (*flow[:5], time.time() - 1)
            with self.assertRaises(Exception):
                self.connector.complete_authorization('code', state, 'http://localhost/callback', 'original')
            self.connector.authorize_url('http://localhost/callback', 'original')
            state = next(iter(self.connector._flows))
            self.connector.complete_authorization('code', state, 'http://localhost/callback', 'original')
        session = self.connector._sessions['original']
        session.expires_at = time.time() - 1
        session.client = SimpleNamespace(refresh=lambda value: SimpleNamespace(access_token='rotated-secret', refresh_token='rotated-refresh', expires_in=3600))
        self.assertEqual(self.connector._require_token('original'), 'rotated-secret')
        self.assertEqual(session.refresh_token, 'rotated-refresh')

    def test_cross_browser_review_and_callback_are_rejected(self):
        """A capability belongs to the initiating browser, including the cross-site OAuth callback."""
        review = self.plan().json()
        self.client.cookies.set('harnest-studio-fused-1940', 'other')
        self.assertEqual(self.client.post('/api/mcp/apply', json={'review': review['mcp_review']}).status_code, 409)
        response = self.client.get('/fused/oauth/callback?code=code&state=forged', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.client.post('/api/mcp/plan', json={}, headers={'Origin': 'https://evil.test'}).status_code, 403)

    def test_real_oauth_flow_is_bound_and_replay_protected(self):
        """The builder accepts a consent redirect without relaxing API same-origin checks."""
        with patch('harnest.fused_connectors.FusedAuthClient', FakeAuthClient), patch.object(self.connector, '_register_client', return_value=('id', 'fos_temporary')):
            started = self.client.post('/api/mcp/connect', json={'engine_url': 'https://engine.test'})
            self.assertEqual(started.status_code, 200, started.text)
            state = next(iter(self.connector._flows))
            callback = self.client.get('/fused/oauth/callback', params={'code': 'code', 'state': state}, headers={'Sec-Fetch-Site': 'cross-site'})
            self.assertEqual(callback.status_code, 200, callback.text)
            self.assertNotIn('access-token', callback.text)
            self.assertEqual(self.client.get('/api/mcp/status').json()['connected'], True)
            self.assertEqual(self.client.get('/fused/oauth/callback', params={'code': 'code', 'state': state}).status_code, 502)

    def test_execution_credentials_only_reach_owned_project_jobs(self):
        """Model runtime configuration stays separate from project execution environments."""
        self.credentials.save(self.project, {'HARNEST_MCP_EXAMPLE_TOKEN': 'job-secret'})
        bindings = self.app.state.jobs._bindings({'project': 'agent'})
        self.assertEqual(bindings, {'HARNEST_MCP_EXAMPLE_TOKEN': 'job-secret'})
        self.assertEqual(Jobs._safe_output([b'token job-sec', b'ret\n'], ['job-secret'], pending=False), 'token [redacted]\n')
        self.assertNotIn('job-sec', Jobs._safe_output([b'token job-sec'], ['job-secret'], pending=True))


class BuilderMCPModelTests(unittest.IsolatedAsyncioTestCase):
    """Exercise AI-to-host discovery and proposal boundaries without an external provider."""

    async def test_discovery_is_opt_in_and_model_can_only_prepare(self):
        """A native discovery envelope is fulfilled before a reviewed plan, never auto-applied."""
        fixture = StudioMCPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calls = []

        async def completion(**options):
            """Script model envelopes and inspect the next-turn discovery context."""
            calls.append(options)
            result = {'fused_discover': {'action': 'services'}} if len(calls) == 1 else {
                'mcp_plan': {'kind': 'existing', 'resource': 'fused', 'name': 'existing', 'version': '1.0.0'}}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])

        app = create_app(fixture.root, '/missing', token='launch', completion=completion, connector=fixture.connector, credentials=fixture.credentials)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://127.0.0.1:1940', headers={'Authorization': 'Bearer launch'}, cookies={'harnest-studio-fused-1940': 'browser'}) as client:
            body = {'project': 'agent', 'model': 'test', 'prompt': 'Connect Fused'}
            denied = await client.post('/api/propose', json=body)
            self.assertEqual(denied.status_code, 422)
            calls.clear()
            result = await client.post('/api/propose', json={**body, 'allow_fused_discovery': True})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn('mcp_review', result.json())
        self.assertIn('verify-service-a', json.dumps(calls[-1]))
        self.assertNotIn('access-token', json.dumps(calls))
        self.assertEqual((ManagementClient.deployments, ManagementClient.tokens), (0, 0))
