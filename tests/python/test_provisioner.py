"""Provisioner configuration and pure backend plans require no infrastructure."""

import json
import unittest

from harnest.provisioner_config import ProvisionError, parse_manifest
from harnest.provisioner_plan import Plan

MANIFEST = """
name: support
services:
  database:
    mode: connect
    url: {secret: DATABASE_URL}
    variable: DATABASE_URL
  cache:
    mode: provision
    type: redis
    image: redis:7.4
    ports: {redis: 6379}
    healthcheck: {command: [redis-cli, ping]}
    persistence: {mount: /data, size: 5Gi}
    provides: {REDIS_URL: 'redis://${services.cache.host}:${services.cache.ports.redis}/0'}
agents:
  worker:
    image: registry.example/worker:1
    healthcheck: {command: [python, -c, 'print(1)']}
    ports: {http: 1907}
    depends_on: [database, cache]
  coordinator:
    image: registry.example/coordinator:1
    healthcheck: {command: [python, -c, 'print(1)']}
    depends_on: [worker]
    environment: {WORKER_URL: 'http://${agents.worker.host}:${agents.worker.ports.http}'}
environments:
  production:
    backend: kubernetes
    context: production-k3s
    namespace: agents
    services:
      cache:
        mode: connect
        url: {secret: REDIS_URL}
        variable: REDIS_URL
"""


class ProvisionerPlanTests(unittest.TestCase):
    """Assert user-visible mode, binding, namespace, and persistence contracts."""

    def plan(self, text=MANIFEST, environment="local"):
        """Use a stable workspace identity to compare deterministic resources."""

        return Plan(parse_manifest(text, environment), environment, "/workspace")

    def test_revision_markers_preserve_selectors_and_unchanged_persistent_services(self):
        """A new release rolls agents without restarting an unchanged database solely for metadata."""

        plan = self.plan()
        credentials = {"DATABASE_URL": "test"}
        before = plan.compose(credentials)
        database = plan.kubernetes("cache", {})[-1]
        worker = plan.kubernetes("worker", credentials)[-1]
        plan.revision = 2
        after = plan.compose(credentials)
        self.assertEqual(before["services"]["cache"], after["services"]["cache"])
        self.assertEqual(after["services"]["worker"]["labels"]["harnest.dev/revision"], "2")
        self.assertEqual(database["spec"], plan.kubernetes("cache", {})[-1]["spec"])
        updated = plan.kubernetes("worker", credentials)[-1]
        self.assertEqual(worker["spec"]["selector"], updated["spec"]["selector"])
        self.assertEqual(updated["spec"]["template"]["metadata"]["annotations"]["harnest.dev/revision"], "2")

    def test_mixed_services_and_agent_dependency_bindings(self):
        """Only selected dependencies inject settings; remote services create no workloads."""

        plan = self.plan()
        document = plan.compose({"DATABASE_URL": "postgres://user:p$a$$@db/app"})
        self.assertEqual(set(document["services"]), {"cache", "worker", "coordinator"})
        worker = document["services"]["worker"]
        self.assertEqual(worker["environment"]["DATABASE_URL"], "postgres://user:p$$a$$$$@db/app")
        self.assertEqual(worker["environment"]["REDIS_URL"], "redis://cache:6379/0")
        self.assertEqual(worker["depends_on"], {"cache": {"condition": "service_healthy"}})
        self.assertEqual(document["services"]["coordinator"]["environment"], {"WORKER_URL": "http://worker:1907"})
        self.assertTrue(document["volumes"])
        self.assertNotIn("postgres://", json.dumps(plan.summary()))

    def test_environment_mode_switch_removes_local_infrastructure(self):
        """A connect overlay replaces image, probes, ports, and volumes together."""

        plan = self.plan(environment="production")
        self.assertEqual(set(plan.workloads), {"worker", "coordinator"})
        self.assertEqual(plan.environment_for("worker", {"DATABASE_URL": "db", "REDIS_URL": "redis"}), {"DATABASE_URL": "db", "REDIS_URL": "redis"})
        self.assertIn(plan.resource_name("worker"), plan.environment_for("coordinator", {})["WORKER_URL"])

    def test_kubernetes_persistence_and_secret_references(self):
        """Persistent workloads avoid overlapping writers; credentials never enter pod env literals."""

        text = MANIFEST.replace("name: support", "name: support\nbackend: kubernetes\ncontext: k3s\nnamespace: agents")
        plan = self.plan(text)
        objects = plan.kubernetes("cache", {})
        pvc = next(obj for obj in objects if obj["kind"] == "PersistentVolumeClaim")
        self.assertNotIn("ownerReferences", pvc["metadata"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "5Gi")
        workload = objects[-1]
        self.assertEqual(workload["spec"]["strategy"], {"type": "Recreate"})
        self.assertFalse(workload["spec"]["template"]["spec"]["automountServiceAccountToken"])
        worker = plan.kubernetes("worker", {"DATABASE_URL": "sensitive"})
        self.assertNotIn("sensitive", json.dumps(worker))
        self.assertTrue(worker[0]["data"])
        self.assertNotIn("stringData", worker[0])
        self.assertEqual(worker[-1]["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["key"], "DATABASE_URL")

    def test_invalid_manifests_are_rejected_without_echoing_values(self):
        """Unknown modes, graph errors, target omissions, and accidental secret fields fail closed."""

        cases = [
            "name: support\nservices: {db: {mode: managed, password: super-secret}}",
            MANIFEST.replace("depends_on: [database, cache]", "depends_on: [missing]"),
            MANIFEST.replace("depends_on: [database, cache]", "depends_on: [coordinator]"),
            MANIFEST.replace("name: support", "name: support\nbackend: kubernetes"),
            MANIFEST.replace("    ports: {redis: 6379}", "    ports: {redis: 70000}"),
            MANIFEST.replace("    image: redis:7.4", "    image: redis:7.4\n    privileged: true"),
            MANIFEST.replace("    image: redis:7.4", "    image: redis:7.4\n    volumes: [/etc:/host]"),
            MANIFEST.replace("    image: registry.example/worker:1", "    image: registry.example/worker:1\n    replicas: 2\n    publish: {http: 1907}"),
        ]
        for text in cases:
            with self.subTest(text=text[:80]):
                with self.assertRaises(ProvisionError) as raised:
                    self.plan(text)
                self.assertNotIn("super-secret", str(raised.exception))

    def test_missing_credentials_and_invalid_placeholders(self):
        """Planning never resolves credentials; apply preparation checks every required binding."""

        plan = self.plan()
        plan.summary()
        with self.assertRaisesRegex(ProvisionError, "DATABASE_URL"):
            plan.compose({})
        for value in ("${services.missing.host}", "${services.database.host}", "${services.cache.ports.nope}", "${ARBITRARY}"):
            with self.subTest(value=value), self.assertRaises(ProvisionError):
                plan.resolve(value, {})

    def test_binding_conflicts_are_not_silently_overwritten(self):
        """Every consumed environment variable has exactly one owner."""

        text = MANIFEST.replace("    depends_on: [database, cache]", "    depends_on: [database, cache]\n    environment: {DATABASE_URL: override}")
        with self.assertRaises(ProvisionError):
            self.plan(text)
        with self.assertRaises(ProvisionError):
            self.plan(MANIFEST.replace("provides: {REDIS_URL:", "provides: {DATABASE_URL:"))

    def test_names_are_scoped_and_valid_for_kubernetes(self):
        """Separate projects and environments cannot share implicit resource ownership."""

        config = parse_manifest(MANIFEST)
        first, second = Plan(config, "local", "/one"), Plan(config, "local", "/two")
        self.assertNotEqual(first.identity, second.identity)
        self.assertLessEqual(len(first.resource_name("a" * 40)), 63)
        self.assertEqual(first.identity, Plan(config, "local", "/one").identity)

    def test_local_ports_are_loopback_only_and_command_is_literal(self):
        """A manifest cannot bind local services publicly or execute Compose interpolation."""

        text = MANIFEST.replace("    ports: {redis: 6379}", "    ports: {redis: 6379}\n    publish: {redis: 16379}\n    command: [redis-server, '$PASSWORD']")
        container = self.plan(text).compose({"DATABASE_URL": "db"})["services"]["cache"]
        self.assertEqual(container["ports"], ["127.0.0.1:16379:6379"])
        self.assertEqual(container["command"], ["redis-server", "$$PASSWORD"])

    def test_duplicate_yaml_keys_and_invalid_port_bindings_are_rejected(self):
        """Ambiguous YAML or unknown host publication cannot silently change deployment intent."""

        with self.assertRaises(ProvisionError):
            self.plan("name: first\nname: second\n")
        with self.assertRaises(ProvisionError):
            self.plan(MANIFEST.replace("    ports: {redis: 6379}", "    ports: {redis: 6379}\n    publish: {missing: 6379}"))

    def test_status_does_not_report_ready_for_missing_replicas(self):
        """Live status must account for every expected component and replica."""

        from harnest.provisioner import runtime_status
        plan = self.plan()
        state = {"status": "ready", "resources": {key: node.image for key, node in plan.workloads.items()}, "summary": plan.summary()}
        components = [{"name": key, "state": "running", "health": "healthy"} for key in plan.workloads]
        self.assertEqual(runtime_status(components, state), "ready")
        self.assertEqual(runtime_status(components[:-1], state), "degraded")
        self.assertEqual(runtime_status([], state), "missing")
        self.assertEqual(runtime_status(components, {**state, "status": "failed"}), "incomplete")
