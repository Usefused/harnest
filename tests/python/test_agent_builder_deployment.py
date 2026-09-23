"""Deployment discovery, source proposals, and native backend rendering from Studio."""

import json
import unittest

import yaml

from test_agent_builder import _DeploymentBuilderFixture as _BuilderFixture
from harnest.provisioner_config import parse_manifest, ProvisionError
from harnest.provisioner_plan import Plan
from harnest_builder.prompting import _context


class BuilderDeploymentTests(_BuilderFixture):
    """Exercise authenticated routes and the shared provisioner without infrastructure mutations."""

    def setup_body(self, **overrides):
        """Supply explicit image and target choices independently of source inference."""
        return {"project": "sample", "name": "sample", "image": "sample:local", "variables": ["DATABASE_URL", "API_KEY"], **overrides}

    def discover_source(self):
        """Create optional storage, a subagent environment read, and an inert MCP command."""
        (self.project / "lifecycle").mkdir()
        (self.project / "lifecycle/storage.py").write_text('import os\nfrom harnest.store import PostgresStore\ndsn = os.environ.get("DATABASE_URL")\n')
        (self.project / "mcp").mkdir()
        (self.project / "mcp/fused.py").write_text('def client():\n    return MCPClient(command="npx")\n')
        (self.project / "subagents").mkdir()
        (self.project / "subagents/helper.py").write_text('import os\nvalue = os.getenv("NESTED_KEY")\nraise RuntimeError("must never run")\n')
        (self.project / "extensions").mkdir()
        (self.project / "extensions/extension.yaml").write_text('endpoint: ${EXTENSION_URL}\n')
        (self.project / "mcp/remote.py").write_text('url = "${REMOTE_MCP_URL}"\n')

    def proposal(self, **kwargs):
        """Obtain proposals through the same authenticated route used by the browser."""
        return self.client.post("/api/deployment/propose", json=self.setup_body(**kwargs))

    def test_discovery_is_static_nested_and_does_not_return_credentials(self):
        """Discover source requirements without imports, linking outside files, or leaking values."""
        self.discover_source()
        config = yaml.safe_load((self.project / "config.yaml").read_text())
        config["spec"]["environment"] = {"API_KEY": "private-test-value", "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}
        (self.project / "config.yaml").write_text(yaml.safe_dump(config))
        result = self.client.get("/api/deployment/inspect", params={"project": "sample"})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertTrue({"API_KEY", "DATABASE_URL", "NESTED_KEY", "EXTENSION_URL", "REMOTE_MCP_URL"} <= set(data["variables"]))
        self.assertEqual(data["programs"], ["npx"])
        self.assertEqual({service["name"] for service in data["services"]}, {"database", "ollama"})
        self.assertNotIn("private-test-value", result.text)
        self.assertIn("Localhost", " ".join(data["warnings"]))

    def test_deployment_discovery_ignores_retired_agent_settings(self):
        """Studio cannot reuse removed config fields as implicit deployment defaults."""

        path = self.project / "config.yaml"
        config = yaml.safe_load(path.read_text())
        for name in ("resources", "scaling"):
            with self.subTest(name=name):
                config["spec"][name] = {"cpu": "100", "memory": "999Gi", "unused": "${IGNORED_VARIABLE}"}
                path.write_text(yaml.safe_dump(config))
                result = self.client.get("/api/deployment/inspect", params={"project": "sample"})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["resources"], {})
                self.assertNotIn("IGNORED_VARIABLE", result.json()["variables"])
                del config["spec"][name]

    def test_local_proposal_then_save_and_plan(self):
        """A complete proposal preserves external services and writes only after explicit review."""
        self.discover_source()
        response = self.proposal(memory="2Gi", cpus=2.0, network_yaml="hosts: {host.docker.internal: host-gateway}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.project / "harnest-deployment.yaml").exists())
        result = response.json()
        changes = [{key: file[key] for key in ("path", "text", "revision")} for file in result["files"]]
        self.assertEqual(self.save(changes).status_code, 200)
        plan = Plan(parse_manifest((self.project / "harnest-deployment.yaml").read_text()), "local", str(self.project))
        compose = plan.compose({"DATABASE_URL": "postgres://external/app", "API_KEY": "value"})
        agent = compose["services"]["sample"]
        self.assertEqual(agent["environment"]["DATABASE_URL"], "postgres://external/app")
        self.assertEqual(agent["deploy"]["resources"]["limits"], {"cpus": "2.0", "memory": "2G"})
        self.assertEqual(agent["extra_hosts"], {"host.docker.internal": "host-gateway"})
        self.assertEqual(set(compose["services"]), {"sample"})
        self.assertEqual(self.save(changes).status_code, 409)

    def test_kubernetes_export_uses_shared_renderer_without_secrets(self):
        """Generate separate multi-document Kubernetes YAML without inventing credential contents."""
        response = self.proposal(backend="kubernetes", context="test-cluster", namespace="agents",
                                 network_yaml="hosts: {internal.example: 10.0.0.12}\ndns: [10.0.0.53]")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        files = {file["path"]: file["text"] for file in data["files"]}
        objects = list(yaml.safe_load_all(files["deploy/kubernetes.yaml"]))
        self.assertEqual([obj["kind"] for obj in objects], ["Service", "Deployment"])
        self.assertNotIn("<secret:", files["deploy/kubernetes.yaml"])
        pod = objects[-1]["spec"]["template"]["spec"]
        self.assertEqual(pod["hostAliases"], [{"ip": "10.0.0.12", "hostnames": ["internal.example"]}])
        self.assertEqual(pod["dnsPolicy"], "None")
        self.assertEqual(pod["dnsConfig"], {"nameservers": ["10.0.0.53"]})
        self.assertNotIn("publish:", files["harnest-deployment.yaml"])
        self.assertEqual(self.save([{key: file[key] for key in ("path", "text", "revision")} for file in data["files"]]).status_code, 200)

    def test_explicit_service_images_ports_and_bindings(self):
        """User-defined provisioned services render alongside inferred external connections."""
        services = """cache:
  mode: provision
  image: redis:7.4
  ports: {redis: 6379}
  healthcheck: {command: [redis-cli, ping]}
  resources: {cpus: 0.5, memory: 256Mi}
  network: {hosts: {internal.example: 10.0.0.12}}
  provides: {REDIS_URL: 'redis://${services.cache.host}:6379/0'}
"""
        response = self.proposal(services_yaml=services)
        self.assertEqual(response.status_code, 200, response.text)
        deployment = parse_manifest(response.json()["files"][0]["text"])
        plan = Plan(deployment, "local", "/workspace")
        self.assertEqual(plan.environment_for("sample", None)["REDIS_URL"], "redis://cache:6379/0")
        self.assertEqual(plan.workloads["cache"].resources.memory, "256Mi")

    def test_wrong_manifest_format_is_actionable_and_raw_kubernetes_can_be_saved(self):
        """Kubernetes streams are supported without confusing the provisioner input contract."""
        text = "apiVersion: apps/v1\nkind: Deployment\n---\napiVersion: v1\nkind: Service\n"
        response = self.save([{"path": "harnest-deployment.yaml", "text": text, "revision": ""}])
        self.assertEqual(response.status_code, 422)
        self.assertIn("deploy/kubernetes.yaml", response.json()["detail"])
        self.assertEqual(self.save([{"path": "deploy/kubernetes.yaml", "text": text, "revision": ""}]).status_code, 200)
        (self.project / "harnest-deployment.yaml").write_text(text)
        response = self.proposal()
        self.assertEqual(response.status_code, 200, response.text)
        backup = next(file for file in response.json()["files"] if file["path"] == "deploy/original-kubernetes.yaml")
        self.assertEqual(backup["text"], text)
        self.assertEqual((self.project / "harnest-deployment.yaml").read_text(), text)

    def test_existing_harnest_configuration_is_not_replaced(self):
        """The generator never silently drops existing hand-authored services and overlays."""
        (self.project / "harnest-deployment.yaml").write_text("name: existing\n")
        self.assertEqual(self.proposal().status_code, 409)

    def test_invalid_target_network_and_services_are_rejected(self):
        """Fail configuration mistakes before writing source or touching a cluster."""
        choices = [
            {"backend": "kubernetes"},
            {"network_yaml": "hosts: {api: not-an-ip}"},
            {"network_yaml": "dns: [bad]"},
            {"backend": "kubernetes", "context": "cluster", "namespace": "agents", "network_yaml": "hosts: {host: host-gateway}"},
            {"services_yaml": "cache: {}\ncache: {}"},
            {"services_yaml": "cache: []"},
            {"services_yaml": "cache: {mode: provision, image: redis:7.4}"},
            {"variables": ["API_KEY=value"]},
        ]
        for kwargs in choices:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.proposal(**kwargs).status_code, 422)
        self.assertFalse((self.project / "harnest-deployment.yaml").exists())

    def test_model_context_contains_current_deployment_contract(self):
        """Model grounding follows the canonical schema including network and resource fields."""
        schema = json.loads(_context([], [], False))["deployment_schema"]
        self.assertIn("Network", schema["$defs"])
        self.assertIn("resources", schema["$defs"]["Agent"]["properties"])

    def test_network_rejects_bad_addresses_at_native_cli_boundary(self):
        """The CLI shares the same network validation as Studio, independent of UI checks."""
        with self.assertRaises(ProvisionError):
            parse_manifest("name: demo\nagents:\n  demo:\n    image: demo:local\n    healthcheck: {command: [true]}\n    network: {hosts: {api: evil.example}}\n")


if __name__ == "__main__":
    unittest.main()
