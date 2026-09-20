"""Validated deployment declarations shared by local and Kubernetes provisioning."""

from __future__ import annotations

from copy import deepcopy
from graphlib import TopologicalSorter, CycleError
from pathlib import Path
from ipaddress import ip_address
from typing import Annotated, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationError
import yaml

MANIFEST = "harnest-deployment.yaml"
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]
PortName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,14}$")]
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class ProvisionError(ValueError):
    """A safe, credential-free provisioning failure suitable for CLI and Studio."""


class StrictModel(BaseModel):
    """Reject ignored configuration and coercions before infrastructure is touched."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SecretReference(StrictModel):
    """Resolve a credential from the provisioner's environment only at apply time."""

    secret: EnvironmentName


Value = str | SecretReference


class Persistence(StrictModel):
    """Declare a retained named volume locally or a retained PVC on Kubernetes."""

    mount: str = Field(pattern=r"^/[^:\x00]+$", max_length=512)
    size: str = Field(default="5Gi", pattern=r"^[1-9][0-9]*(Mi|Gi|Ti)$")
    storage_class: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]*$")


class Healthcheck(StrictModel):
    """Use the same explicit exec probe for Compose and Kubernetes readiness."""

    command: list[str] = Field(min_length=1, max_length=32)
    interval: int = Field(default=5, ge=1, le=300)
    timeout: int = Field(default=3, ge=1, le=300)
    retries: int = Field(default=20, ge=1, le=120)


class Resources(StrictModel):
    """Bound container CPU and memory consistently across deployment backends."""

    cpus: float = Field(default=1.0, gt=0, le=128)
    memory: str = Field(default="512Mi", pattern=r"^[1-9][0-9]*(Mi|Gi)$")


class Network(StrictModel):
    """Declare per-workload host aliases and DNS without granting host networking."""

    hosts: dict[Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$")], str] = Field(default_factory=dict, max_length=32)
    dns: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_addresses(self):
        """Accept IP addresses and Compose's explicit host-gateway alias only."""
        for value in self.hosts.values():
            if value != "host-gateway":
                ip_address(value)
        for value in self.dns:
            ip_address(value)
        return self


class Container(StrictModel):
    """Describe one independently deployable image, with no host mounts or privileges."""

    image: str = Field(min_length=1, max_length=300, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$")
    command: list[str] = Field(default_factory=list, max_length=32)
    ports: dict[PortName, Annotated[int, Field(ge=1, le=65535)]] = Field(default_factory=dict, max_length=16)
    publish: dict[PortName, Annotated[int, Field(ge=1024, le=65535)]] = Field(default_factory=dict, max_length=16)
    environment: dict[EnvironmentName, Value] = Field(default_factory=dict, max_length=64)
    depends_on: list[Name] = Field(default_factory=list, max_length=32)
    healthcheck: Healthcheck
    persistence: Persistence | None = None
    resources: Resources = Field(default_factory=Resources)
    network: Network = Field(default_factory=Network)

    @model_validator(mode="after")
    def check_ports(self):
        """A published port must refer to a declared port, never an arbitrary host binding."""

        if self.publish.keys() - self.ports.keys():
            raise ValueError("publish references an undeclared port")
        if len(set(self.ports.values())) != len(self.ports):
            raise ValueError("duplicate container ports")
        return self


class Agent(Container):
    """Give each agent image its own workload and explicit dependency bindings."""

    replicas: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def check_replicas(self):
        """Single-writer volumes and fixed host ports cannot safely share replicas."""

        if self.replicas > 1 and (self.persistence or self.publish):
            raise ValueError("replicated agents cannot use persistence or fixed published ports")
        return self


class ProvisionedService(Container):
    """Run a user-selected database, cache, or custom service image."""

    mode: Literal["provision"]
    type: str = Field(default="custom", max_length=64)
    provides: dict[EnvironmentName, Value] = Field(default_factory=dict, max_length=64)


class ConnectedService(StrictModel):
    """Attach externally owned services without provisioning or deleting infrastructure."""

    mode: Literal["connect"]
    type: str = Field(default="custom", max_length=64)
    provides: dict[EnvironmentName, Value] = Field(default_factory=dict, max_length=64)
    url: Value | None = None
    variable: EnvironmentName | None = None

    @model_validator(mode="after")
    def check_connection(self):
        """Support a single URL binding or the template-compatible provides mapping."""

        if (self.url is None) != (self.variable is None):
            raise ValueError("url and variable must be supplied together")
        if self.url is None and not self.provides:
            raise ValueError("connected services require url/variable or provides")
        if self.variable in self.provides:
            raise ValueError("duplicate connection binding")
        return self

    def bindings(self) -> dict[str, Value]:
        """Normalize URL shorthand without changing the authored declaration."""

        return {**self.provides, **({self.variable: self.url} if self.variable else {})}


Service = Annotated[ProvisionedService | ConnectedService, Field(discriminator="mode")]


class Deployment(StrictModel):
    """Select one explicit target and a dependency graph of agents and services."""

    version: Literal[1] = 1
    name: Name
    release: str | None = Field(default=None, min_length=1, max_length=100)
    backend: Literal["local", "kubernetes"] = "local"
    context: str | None = Field(default=None, min_length=1, max_length=200)
    namespace: Name | None = None
    services: dict[Name, Service] = Field(default_factory=dict, max_length=32)
    agents: dict[Name, Agent] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def check_graph(self):
        """Reject target ambiguity, collisions, unknown dependencies, and cycles."""

        if self.backend == "kubernetes" and not (self.context and self.namespace):
            raise ValueError("Kubernetes requires an explicit context and existing namespace")
        if self.backend == "local" and (self.context or self.namespace):
            raise ValueError("local deployments cannot select a Kubernetes target")
        if self.services.keys() & self.agents.keys():
            raise ValueError("agent and service names must be unique")
        self.check_network_target()
        self.order()
        return self

    def check_network_target(self) -> None:
        """Reject Compose-specific aliases before any Kubernetes rendering or mutations."""
        if self.backend == "kubernetes" and any("host-gateway" in node.network.hosts.values() for node in self.workloads().values()):
            raise ValueError("host-gateway is local-only; use a reachable IP address for Kubernetes")

    def workloads(self) -> dict[str, Container]:
        """Exclude connected services from every infrastructure operation."""

        return {**{k: v for k, v in self.services.items() if isinstance(v, ProvisionedService)}, **self.agents}

    def order(self) -> list[str]:
        """Topologically order readiness gates before their consuming workloads."""

        nodes = {**self.services, **self.agents}
        graph = {key: getattr(value, "depends_on", []) for key, value in nodes.items()}
        if any(set(deps) - nodes.keys() for deps in graph.values()):
            raise ValueError("unknown dependency")
        try:
            return list(TopologicalSorter(graph).static_order())
        except CycleError:
            raise ValueError("dependency cycle") from None


class ManifestLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently selecting an unintended deployment."""

    def construct_mapping(self, node, deep=False):
        """Validate uniqueness before the safe loader constructs nested configuration."""

        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate deployment key")
        return super().construct_mapping(node, deep=deep)


def _merge(base: dict, override: dict) -> dict:
    """Replace mode changes completely so local container fields cannot leak into connect mode."""

    result = deepcopy(base)
    for key, value in override.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict) and value.get("mode", previous.get("mode")) == previous.get("mode"):
            result[key] = _merge(previous, value)
        else:
            result[key] = deepcopy(value)
    return result


def parse_manifest(text: str, environment: str = "local") -> Deployment:
    """Apply one environment overlay and return only sanitized validation diagnostics."""

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment):
        raise ProvisionError("Invalid deployment environment name")
    try:
        data = yaml.load(text, Loader=ManifestLoader)
        if not isinstance(data, dict):
            raise ValueError("mapping required")
        overrides = data.pop("environments", {})
        if not isinstance(overrides, dict):
            raise ValueError("environments must be a mapping")
        if environment != "local" and environment not in overrides:
            raise ProvisionError("Unknown deployment environment")
        return Deployment.model_validate(_merge(data, _overlay(overrides, environment)))
    except ValidationError as error:
        fields = [".".join(map(str, item["loc"])) or "deployment" for item in error.errors()]
        raise ProvisionError("Invalid deployment configuration at: " + ", ".join(fields)) from None
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise ProvisionError("Invalid deployment manifest or environment") from None


def load_manifest(root: Path, environment: str = "local") -> Deployment:
    """Read a bounded, unlinked manifest from the selected project."""

    path = root / MANIFEST
    if path.is_symlink():
        raise ProvisionError("Deployment manifest cannot be a symbolic link")
    try:
        with path.open() as stream:
            text = stream.read(1_048_577)
        if len(text.encode()) > 1_048_576:
            raise ProvisionError("Deployment manifest exceeds 1 MiB")
        return parse_manifest(text, environment)
    except OSError:
        raise ProvisionError("Create harnest-deployment.yaml in the project first") from None


def _overlay(overrides: dict, environment: str) -> dict:
    """Keep overlay shape checks separate from credential-safe diagnostics."""

    value = overrides.get(environment, {})
    if not isinstance(value, dict):
        raise ValueError("environment must be a mapping")
    return value
