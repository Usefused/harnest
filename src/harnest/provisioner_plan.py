"""Pure connection resolution and resource rendering for provisioning backends."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Mapping

from .provisioner_config import ConnectedService, Container, Deployment, ProvisionError, SecretReference
from .provisioner_access import agent_access

OWNER_LABEL = "harnest.dev/deployment"
_REFERENCE = re.compile(r"\$\{(services|agents)\.([a-z][a-z0-9-]*)\.(host|ports\.[a-z][a-z0-9-]*)\}")


class Plan:
    """Keep credentials out of previews; resolve them only in ephemeral apply payloads."""

    def __init__(self, deployment: Deployment, environment: str, owner: str):
        """Derive bounded resource names scoped to the project and selected environment."""

        self.deployment = deployment
        self.environment = environment
        self.revision: int | None = None
        self.identity = "harnest-" + hashlib.sha256(f"{owner}:{deployment.name}:{environment}".encode()).hexdigest()[:14]
        self.workloads = deployment.workloads()
        # Validate bindings even for previews, before constructing any remote resources.
        for name in self.workloads:
            self.environment_for(name, None)

    def resource_name(self, name: str) -> str:
        """Keep generated identifiers below the Kubernetes DNS label limit."""

        return f"{self.identity}-{name}"

    def labels(self, name: str | None = None) -> dict[str, str]:
        """Use one ownership selector across all created resources and status queries."""

        result = {OWNER_LABEL: self.identity}
        if name:
            result["harnest.dev/component"] = name
        return result

    def revision_metadata(self) -> dict[str, str]:
        """Expose revision identity without changing immutable workload or Service selectors."""

        return {"harnest.dev/revision": str(self.revision)} if self.revision is not None else {}

    def workload_revision(self, name: str, node: Container) -> dict[str, str]:
        """Do not restart an unchanged persistent database just to update an agent release marker."""

        if name in self.deployment.services and node.persistence:
            return {}
        return self.revision_metadata()

    def resolve(self, value, secrets: Mapping[str, str] | None) -> str:
        """Resolve secret references and service DNS placeholders without evaluating expressions."""

        if isinstance(value, SecretReference):
            if secrets is None:
                return "<secret:" + value.secret + ">"
            if not secrets.get(value.secret):
                raise ProvisionError("Missing required secret: " + value.secret)
            return secrets[value.secret]
        result = _REFERENCE.sub(self._reference, value)
        if "${" in result:
            raise ProvisionError("Unknown connection placeholder; use services.NAME.host or services.NAME.ports.PORT")
        return result

    def _reference(self, match: re.Match) -> str:
        """Use backend DNS names; external service endpoints must be supplied explicitly."""

        group, name, field = match.groups()
        nodes = getattr(self.deployment, group)
        node = nodes.get(name)
        if node is None or isinstance(node, ConnectedService):
            raise ProvisionError("Connection placeholder must reference a provisioned component")
        if field == "host":
            if not node.ports:
                raise ProvisionError("Connection host references require a declared service port")
            return name if self.deployment.backend == "local" else self.resource_name(name)
        port = node.ports.get(field.removeprefix("ports."))
        if port is None:
            raise ProvisionError("Connection placeholder references an undeclared port")
        return str(port)

    def environment_for(self, name: str, secrets: Mapping[str, str] | None) -> dict[str, str]:
        """Inject only explicitly declared service dependencies; reject ambiguous bindings."""

        component = self.workloads[name]
        result = {}
        for dependency in component.depends_on:
            service = self.deployment.services.get(dependency)
            bindings = service.bindings() if isinstance(service, ConnectedService) else getattr(service, "provides", {})
            if result.keys() & bindings.keys():
                raise ProvisionError("Dependencies provide duplicate environment variables")
            result.update({key: self.resolve(value, secrets) for key, value in bindings.items()})
        if result.keys() & component.environment.keys():
            raise ProvisionError("A dependency binding conflicts with container environment")
        result.update({key: self.resolve(value, secrets) for key, value in component.environment.items()})
        return result

    def summary(self) -> dict:
        """Return reviewable intent and generated access instructions without credential values."""

        return {
            "name": self.deployment.name, "identity": self.identity, "release": self.deployment.release,
            "environment": self.environment, "backend": self.deployment.backend,
            "context": self.deployment.context, "namespace": self.deployment.namespace,
            "components": [self._summary(name) for name in self.deployment.order()],
            "access": agent_access(self),
            "data_policy": "Persistent volumes are retained when workloads are removed.",
        }

    def _summary(self, name: str) -> dict:
        """Identify ownership and required credential names without resolving their values."""

        node = {**self.deployment.services, **self.deployment.agents}[name]
        if isinstance(node, ConnectedService):
            return {"name": name, "mode": "connect", "variables": sorted(node.bindings()), "managed": False}
        return {"name": name, "mode": "provision", "image": node.image,
                "kind": "agent" if name in self.deployment.agents else "service",
                "replicas": getattr(node, "replicas", 1), "persistent": node.persistence is not None,
                "depends_on": node.depends_on, "variables": sorted(self.environment_for(name, None))}

    def compose(self, secrets: Mapping[str, str]) -> dict:
        """Render isolated local workloads with loopback ports and retained named volumes."""

        result = {"name": self.identity, "services": {}, "volumes": {}}
        for name, node in self.workloads.items():
            container = self._compose_container(name, node, secrets)
            if node.persistence:
                volume = self.resource_name(name) + "-data"
                result["volumes"][volume] = {"name": volume, "labels": self.labels(name)}
                container["volumes"] = [f"{volume}:{node.persistence.mount}"]
            result["services"][name] = container
        # Compose interpolation is not a second configuration language: dollar signs
        # in authored commands or resolved passwords must reach the container literally.
        return _escape_compose(result)

    def _compose_container(self, name: str, node: Container, secrets: Mapping[str, str]) -> dict:
        """Wait for dependency health and apply matching resource limits locally."""

        result = {
            "image": node.image, "restart": "unless-stopped", "labels": {**self.labels(name), **self.workload_revision(name, node)},
            "environment": self.environment_for(name, secrets),
            "ports": [f"127.0.0.1:{host}:{node.ports[port]}" for port, host in node.publish.items()],
            "healthcheck": {"test": ["CMD", *node.healthcheck.command], "interval": f"{node.healthcheck.interval}s",
                            "timeout": f"{node.healthcheck.timeout}s", "retries": node.healthcheck.retries},
            "deploy": {"replicas": getattr(node, "replicas", 1), "resources": {"limits": {
                "cpus": str(node.resources.cpus), "memory": node.resources.memory.replace("i", "")}}},
            "depends_on": {key: {"condition": "service_healthy"} for key in node.depends_on if key in self.workloads},
        }
        if node.command:
            result["command"] = node.command
        if node.network.hosts:
            result["extra_hosts"] = dict(node.network.hosts)
        if node.network.dns:
            result["dns"] = list(node.network.dns)
        return result

    def kubernetes(self, name: str, secrets: Mapping[str, str]) -> list[dict]:
        """Render one component's credentials, storage, service, and managed pod workload."""

        node = self.workloads[name]
        environment = self.environment_for(name, secrets)
        items = [self._object("v1", "Secret", name, {"type": "Opaque", "data": {key: base64.b64encode(value.encode()).decode() for key, value in environment.items()}})]
        if node.persistence:
            items.append(self._volume(name, node))
        if node.ports:
            items.append(self._object("v1", "Service", name, {"spec": {
                "selector": self.labels(name), "ports": [
                    {"name": port, "port": number, "targetPort": number} for port, number in node.ports.items()]}}))
        items.append(self._workload(name, node, environment))
        return items

    def _object(self, version: str, kind: str, name: str, fields: dict) -> dict:
        """Stamp every resource with namespace and ownership for bounded cleanup."""

        return {"apiVersion": version, "kind": kind, "metadata": {
            "name": self.resource_name(name), "namespace": self.deployment.namespace,
            "labels": self.labels(name), "annotations": self.revision_metadata()}, **fields}

    def _volume(self, name: str, node: Container) -> dict:
        """Leave PVCs without owner references so deleting workloads preserves data."""

        spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": node.persistence.size}}}
        if node.persistence.storage_class is not None:
            spec["storageClassName"] = node.persistence.storage_class
        return self._object("v1", "PersistentVolumeClaim", name, {"spec": spec})

    def _workload(self, name: str, node: Container, environment: dict) -> dict:
        """Use replacement updates for single-writer volumes and rolling updates otherwise."""

        container = {"name": name, "image": node.image, "env": [
            {"name": key, "valueFrom": {"secretKeyRef": {"name": self.resource_name(name), "key": key}}} for key in sorted(environment)],
            "readinessProbe": {"exec": {"command": node.healthcheck.command},
                               "periodSeconds": node.healthcheck.interval, "timeoutSeconds": node.healthcheck.timeout,
                               "failureThreshold": node.healthcheck.retries},
            "resources": {"limits": {"cpu": str(node.resources.cpus), "memory": node.resources.memory}},
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["NET_RAW"]}},
        }
        if node.command:
            # Compose command replaces image CMD, so Kubernetes must use args, not command.
            container["args"] = node.command
        pod = {"automountServiceAccountToken": False, "containers": [container]}
        self._pod_network(pod, node)
        if node.persistence:
            container["volumeMounts"] = [{"name": "data", "mountPath": node.persistence.mount}]
            pod["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": self.resource_name(name)}}]
        # Secret changes also roll the workload; kubectl never needs to print the values.
        revision = hashlib.sha256(str(sorted(environment.items())).encode()).hexdigest()
        return self._object("apps/v1", "Deployment", name, {"spec": {
            "replicas": getattr(node, "replicas", 1), "selector": {"matchLabels": self.labels(name)},
            "strategy": {"type": "Recreate" if node.persistence else "RollingUpdate"},
            "template": {"metadata": {"labels": self.labels(name), "annotations": {"harnest.dev/environment": revision, **self.workload_revision(name, node)}}, "spec": pod},
        }})

    def _pod_network(self, pod: dict, node: Container) -> None:
        """Apply explicit per-pod aliases and DNS while leaving cluster defaults intact otherwise."""
        if node.network.hosts:
            pod["hostAliases"] = [{"ip": address, "hostnames": [host]} for host, address in sorted(node.network.hosts.items())]
        if node.network.dns:
            pod["dnsPolicy"] = "None"
            pod["dnsConfig"] = {"nameservers": list(node.network.dns)}


def _escape_compose(value):
    """Escape only string values, preserving object keys used as stable identities."""

    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [_escape_compose(item) for item in value]
    if isinstance(value, dict):
        return {key: _escape_compose(item) for key, item in value.items()}
    return value
