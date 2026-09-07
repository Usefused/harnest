"""Validate bounded public Docker topology declarations without a daemon."""

from __future__ import annotations

import pytest

from harnest.sandbox import SandboxBudget, SandboxPolicyUnsupportedError
from harnest_extension_docker.lib.topology import (
    DockerNetwork,
    DockerReadiness,
    DockerService,
    topology_config,
)


def test_topology_snapshots_services_and_defaults_to_an_internal_bridge() -> None:
    """Keep mutable caller input out of the runtime configuration snapshot."""

    environment = {"MODE": "worker"}
    service = DockerService(
        name="queue",
        image="redis@sha256:test",
        command=["redis-server", "--save", ""],
        environment=environment,
        ports=[6379, 6379],
        aliases=["cache"],
        readiness=DockerReadiness(command=["redis-cli", "ping"]),
        budget=SandboxBudget(memory_bytes=64 * 1024 * 1024),
    )
    environment["MODE"] = "changed"

    config = topology_config([service], None, external_network=False)

    assert config["network"] == {"internal": True}
    assert config["services"][0]["environment"] == {"MODE": "worker"}
    assert config["services"][0]["ports"] == [6379]
    assert config["services"][0]["aliases"] == ["cache"]


def test_non_internal_topology_requires_explicit_egress_authority() -> None:
    """A service network cannot silently widen a no-network sandbox policy."""

    service = DockerService(name="api", image="service@sha256:test")
    with pytest.raises(SandboxPolicyUnsupportedError, match="unrestricted"):
        topology_config(
            [service], DockerNetwork(internal=False), external_network=False
        )
    assert topology_config(
        [service], DockerNetwork(internal=False), external_network=True
    )["network"] == {"internal": False}


@pytest.mark.parametrize(
    ("services", "message"),
    [
        (
            [
                DockerService(name="api", image="one"),
                DockerService(name="worker", image="two", aliases=["api"]),
            ],
            "unique",
        ),
        ([DockerService(name=f"service-{index}", image="image") for index in range(9)], "at most"),
    ],
)
def test_topology_rejects_ambiguous_or_unbounded_service_sets(
    services: list[DockerService], message: str
) -> None:
    """Bound resource fan-out and reserve each internal DNS identity once."""

    with pytest.raises(ValueError, match=message):
        topology_config(services, None, external_network=False)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DockerService(name="Bad_Name", image="image"),
        lambda: DockerService(name="api", image="one", docker_path="two"),
        lambda: DockerService(name="api", image="image", ports=[0]),
        lambda: DockerService(name="api", image="image", environment={"BAD-NAME": "x"}),
        lambda: DockerReadiness(command=[]),
        lambda: DockerReadiness(command=["ready"], retries=0),
        lambda: DockerNetwork(internal=1),
    ],
)
def test_invalid_topology_fields_fail_before_docker(factory) -> None:
    """Reject permissive coercion and provider escape values during authoring."""

    with pytest.raises((TypeError, ValueError)):
        factory()
