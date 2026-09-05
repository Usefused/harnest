"""Verify the public extension factory against Harnest sandbox contracts."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from harnest.sandbox import (
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxRequest,
    SandboxResult,
)
from harnest_extension_docker.extension import (
    DockerSandboxProvider,
    docker_sandbox,
)


def test_definition_is_lazy_and_preserves_adapter_options() -> None:
    """Configuration uses public Sandbox.provider without contacting Docker."""

    definition = docker_sandbox(
        image="python:3.12",
        options={"error_retry_attempts": 2},
        scope="session",
    )
    assert definition.backend == "docker"
    assert definition.timeout_seconds == 300
    assert definition.network_policy == SandboxNetworkPolicy.none()
    assert definition.to_adk_executor().error_retry_attempts == 2
    provider = definition.build()
    assert isinstance(provider, DockerSandboxProvider)
    assert provider._backend._scope == "session"
    assert provider._backend._config["network_enabled"] is False
    provider.close()


def test_unrestricted_policy_maps_only_to_docker_bridge() -> None:
    """The portable unrestricted mode grants Docker's bridge network explicitly."""

    definition = docker_sandbox(
        docker_path="sandbox/image",
        network_policy=SandboxNetworkPolicy.unrestricted(),
    )
    provider = definition.build()
    assert provider._backend._config["network_enabled"] is True
    assert provider._backend._config["docker_path"] == "sandbox/image"
    provider.close()


@pytest.mark.parametrize(
    "policy",
    [
        SandboxNetworkPolicy.allowlist("packages.example", ports=(443,)),
        SandboxNetworkPolicy.unrestricted(block_private_networks=True),
    ],
)
def test_unsupported_network_controls_fail_before_provider_start(
    policy: SandboxNetworkPolicy,
) -> None:
    """Never claim host filtering that Docker bridge selection cannot enforce."""

    with pytest.raises(SandboxPolicyUnsupportedError):
        docker_sandbox(image="python:3.12", network_policy=policy)


def test_provider_rejects_request_policy_substitution() -> None:
    """A direct caller cannot widen policy after Docker was configured."""

    backend = Mock()
    backend.execute.return_value = SandboxResult(stdout="work\n")
    provider = DockerSandboxProvider(backend, SandboxNetworkPolicy.none())
    widened = SandboxRequest(
        "print('work')",
        network_policy=SandboxNetworkPolicy.unrestricted(),
    )
    with pytest.raises(SandboxPolicyUnsupportedError, match="retain"):
        provider.execute(widened)
    backend.execute.assert_not_called()
