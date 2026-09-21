"""Guided deployment review, endpoint derivation, and stale-plan protection."""

import json
import os
import shlex
from unittest.mock import patch

import yaml

from harnest.provisioner import Provisioner
from harnest.provisioner_config import parse_manifest
from harnest.provisioner_plan import Plan
from test_agent_builder import _DeploymentBuilderFixture as _BuilderFixture
from test_provisioner import MANIFEST
from test_provisioner_lifecycle import FakeBackend


class DeploymentOverviewTests(_BuilderFixture):
    """Keep planned addresses distinct from recorded releases and actual backend readiness."""

    def worker_ports(self, summary):
        """Locate an agent independently of YAML mapping serialization order."""
        return next(item["ports"] for item in summary["access"] if item["name"] == "worker")

    def manifest(self, backend="local"):
        """Create a real multi-agent manifest with one published HTTP endpoint."""
        document = yaml.safe_load(MANIFEST)
        document["agents"]["worker"]["publish"] = {"http": 2907}
        document.update(backend=backend)
        if backend == "kubernetes":
            document.update(context="team's cluster; test", namespace="agents")
            document["agents"]["worker"]["publish"] = {}
        path = self.project / "harnest-deployment.yaml"
        path.write_text(yaml.safe_dump(document))
        return path

    def test_local_access_matches_published_port_and_omits_connected_service_urls(self):
        """A configured port is not host access unless it is actually published."""
        path = self.manifest()
        plan = Plan(parse_manifest(path.read_text()), "local", str(self.project))
        result = plan.summary()
        agents = {item["name"]: item for item in result["access"]}
        worker, coordinator = agents["worker"], agents["coordinator"]
        self.assertEqual(worker["ports"][0]["url"], "http://127.0.0.1:2907")
        self.assertEqual(worker["ports"][0]["container_port"], 1907)
        self.assertEqual(coordinator["ports"], [])
        self.assertIn("No network ports", coordinator["note"])
        self.assertNotIn("DATABASE_URL=", json.dumps(result))
        self.assertEqual(set(agents), {"worker", "coordinator"})

    def test_internal_and_non_http_ports_do_not_invent_browser_links(self):
        """TCP ports remain connection coordinates, and unpublished agents remain internal."""
        path = self.manifest()
        document = yaml.safe_load(path.read_text())
        document["agents"]["worker"]["ports"] = {"rpc": 5000, "http": 1907}
        document["agents"]["worker"]["publish"] = {"rpc": 5001}
        plan = Plan(parse_manifest(yaml.safe_dump(document)), "local", str(self.project))
        ports = {item["name"]: item for item in self.worker_ports(plan.summary())}
        self.assertIsNone(ports["rpc"]["url"])
        self.assertEqual(ports["rpc"]["port"], 5001)
        self.assertEqual(ports["http"]["scope"], "internal")
        self.assertIsNone(ports["http"]["url"])

    def test_kubernetes_access_requires_explicit_scoped_port_forward(self):
        """Quoted context names cannot alter the generated command; cluster DNS is not public access."""
        path = self.manifest("kubernetes")
        plan = Plan(parse_manifest(path.read_text()), "local", str(self.project))
        endpoint = self.worker_ports(plan.summary())[0]
        argv = shlex.split(endpoint["command"])
        self.assertEqual(argv, ["kubectl", "--context", "team's cluster; test", "--namespace", "agents",
                               "port-forward", "--address", "127.0.0.1", "service/" + plan.resource_name("worker"), "1907:1907"])
        self.assertEqual(endpoint["scope"], "port-forward")
        self.assertIn("No public ingress", endpoint["note"])

    def test_overview_exposes_missing_variable_names_without_values_or_backend_calls(self):
        """Reading a dashboard cannot deploy images or leak configured service credentials."""
        self.manifest()
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://private-value", "HARNEST_ENABLE_DEPLOYMENT": "true"}, clear=True):
            response = self.client.get("/api/deployment/overview", params={"project": "sample"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["required_variables"], ["DATABASE_URL"])
        self.assertEqual(value["missing_variables"], [])
        self.assertNotIn("private-value", response.text)
        self.assertEqual(value["recorded"]["status"], "not-deployed")
        self.assertTrue(value["manifest_revision"])
        with patch.dict(os.environ, {"HARNEST_ENABLE_DEPLOYMENT": "true"}, clear=True):
            value = self.client.get("/api/deployment/overview", params={"project": "sample"}).json()
        self.assertEqual(value["missing_variables"], ["DATABASE_URL"])
        self.assertIn("DATABASE_URL", value["blockers"][0])

    def test_apply_and_rollback_preserve_exact_release_access_after_source_edits(self):
        """Status and rollback report deployed ports rather than the latest authored port mapping."""
        path = self.manifest()
        service = Provisioner(self.project, runner=FakeBackend())
        first = service.apply({"DATABASE_URL": "test"})
        self.assertEqual(self.worker_ports(first["deployment"])[0]["port"], 2907)
        document = yaml.safe_load(path.read_text())
        document["agents"]["worker"]["publish"] = {"http": 3907}
        path.write_text(yaml.safe_dump(document))
        self.assertEqual(self.worker_ports(service.recorded()["deployment"])[0]["port"], 2907)
        self.assertEqual(self.worker_ports(service.status()["deployment"])[0]["port"], 2907)
        service.apply({"DATABASE_URL": "test"})
        result = service.rollback(1, {"DATABASE_URL": "test"})
        self.assertEqual(self.worker_ports(result["deployment"])[0]["port"], 2907)
        page = self.client.get("/api/deployment/history", params={"project": "sample", "before": 3}).json()
        self.assertEqual([item["revision"] for item in page["revisions"]], [2, 1])

    def test_deploy_rejects_a_manifest_changed_after_review(self):
        """The guided apply button carries the reviewed file revision to the command boundary."""
        path = self.manifest()
        value = self.client.get("/api/deployment/overview", params={"project": "sample"}).json()
        body = {"action": "provision", "operation": "apply", "project": "sample", "manifest_revision": value["manifest_revision"]}
        with patch.object(self.app.state.jobs, "start", return_value={"id": "test"}) as start:
            self.assertEqual(self.client.post("/api/command", json=body).status_code, 200)
            path.write_text(path.read_text() + "\n# edited after review\n")
            self.assertEqual(self.client.post("/api/command", json=body).status_code, 409)
            self.assertEqual(start.call_count, 1)

    def test_invalid_config_and_environment_have_actionable_errors(self):
        """Broken configuration stays editable and invalid history cursors do not trigger commands."""
        path = self.manifest()
        path.write_text("not: supported")
        self.assertEqual(self.client.get("/api/deployment/overview", params={"project": "sample"}).status_code, 422)
        self.assertEqual(self.client.get("/api/deployment/overview", params={"project": "sample", "environment": "../other"}).status_code, 422)
        self.assertEqual(self.client.get("/api/deployment/history", params={"project": "sample", "before": -1}).status_code, 422)
