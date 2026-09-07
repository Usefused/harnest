"""Application-local Docker Harnest Extension contract tests."""

from __future__ import annotations

from pathlib import Path
import shutil
from unittest.mock import Mock, patch

import pytest

from harnest.plugins import activate_runtime_plugins, release_runtime_plugins
from harnest.runtime_plugins import discover_application_extensions
from harnest.sandbox import (
    SandboxNetworkPolicy,
    SandboxPolicyUnsupportedError,
    SandboxRequest,
    SandboxResult,
)


_SOURCE = Path(__file__).parents[2] / "official-extensions" / "docker"


@pytest.fixture
def docker_extension(tmp_path: Path):
    """Activate the checked-in extension through its real public namespace."""

    target = tmp_path / "extensions" / "docker"
    target.parent.mkdir()
    shutil.copytree(_SOURCE, target)
    descriptors = discover_application_extensions(tmp_path)
    activated = activate_runtime_plugins(descriptors)
    try:
        yield activated[0].module, descriptors[0]
    finally:
        release_runtime_plugins(descriptors)


def test_docker_extension_declares_provider_authority(docker_extension) -> None:
    """Keep packaging, dependency, and runtime authority explicit."""

    _module, descriptor = docker_extension
    assert descriptor.name == "docker"
    assert descriptor.capabilities == ("sandbox.provider",)
    assert descriptor.dependencies == (
        "docker<8,>=7.1",
        "harnest<0.16,>=0.15",
    )


def test_factory_maps_no_network_to_extension_owned_backend(docker_extension) -> None:
    """Preserve isolation settings without calling a removed core factory."""

    module, _descriptor = docker_extension
    backend = Mock()
    backend.execute.return_value = SandboxResult(stdout="generic workload\n")
    configured = Mock()
    configured.new_backend.return_value = backend
    policy = SandboxNetworkPolicy.none()

    with patch.object(module, "create_docker_backend", return_value=configured) as create:
        definition = module.docker_sandbox(
            image="python@sha256:test",
            network_policy=policy,
            scope=module.DockerScope.SESSION,
            options={"error_retry_attempts": 2},
        )

    assert definition.backend == "docker"
    assert definition._executor_options == {"error_retry_attempts": 2}
    provider = definition.build()
    request = SandboxRequest(code="print('work')", network_policy=policy)
    assert provider.execute(request).stdout == "generic workload\n"
    configured.new_backend.assert_called_once_with()
    create.assert_called_once_with(
        image="python@sha256:test",
        docker_path=None,
        base_url=None,
        external_network=False,
        services=(),
        network=None,
        timeout_seconds=300,
        max_output_bytes=1_048_576,
        scope=module.DockerScope.SESSION,
        budget=None,
        max_scopes=8,
    )


def test_factory_maps_unrestricted_network_without_browser_policy(
    docker_extension,
) -> None:
    """Translate the other supported generic mode to Docker bridge networking."""

    module, _descriptor = docker_extension
    configured = Mock()
    configured.new_backend.return_value = Mock()
    with patch.object(module, "create_docker_backend", return_value=configured) as create:
        definition = module.docker.sandbox(
            docker_path="sandbox/image",
            network_policy=SandboxNetworkPolicy.unrestricted(),
        )

    assert definition.network_policy == SandboxNetworkPolicy.unrestricted()
    assert create.call_args.kwargs["external_network"] is True


def test_factory_exposes_typed_multi_container_topology(docker_extension) -> None:
    """Keep generic service and network declarations on the extension surface."""

    module, _descriptor = docker_extension
    configured = Mock()
    configured.new_backend.return_value = Mock()
    readiness = module.docker.readiness(command=["service", "ready"])
    service = module.docker.service(
        name="worker",
        image="worker@sha256:test",
        ports=[8080],
        aliases=["jobs"],
        readiness=readiness,
    )
    network = module.docker.network(internal=True)

    with patch.object(module, "create_docker_backend", return_value=configured) as create:
        definition = module.docker.sandbox(
            image="python@sha256:test",
            services=[service],
            network=network,
        )

    assert definition.network_policy == SandboxNetworkPolicy.none()
    assert isinstance(service, module.DockerService)
    assert isinstance(network, module.DockerNetwork)
    assert create.call_args.kwargs["services"] == [service]
    assert create.call_args.kwargs["network"] == network


@pytest.mark.parametrize(
    "policy",
    [
        SandboxNetworkPolicy.allowlist("packages.example", ports=(443,)),
        SandboxNetworkPolicy.unrestricted(block_private_networks=True),
    ],
)
def test_factory_fails_closed_for_unimplemented_network_controls(
    docker_extension, policy: SandboxNetworkPolicy,
) -> None:
    """Never claim enforcement that Docker bridge selection does not provide."""

    module, _descriptor = docker_extension
    with patch.object(module, "create_docker_backend") as create:
        with pytest.raises(SandboxPolicyUnsupportedError):
            module.docker_sandbox(image="python:3.12", network_policy=policy)
    create.assert_not_called()


def test_provider_rejects_request_policy_substitution(docker_extension) -> None:
    """Keep direct provider callers from widening an already configured sandbox."""

    module, _descriptor = docker_extension
    backend = Mock()
    provider = module.DockerSandboxProvider(backend, SandboxNetworkPolicy.none())
    request = SandboxRequest(
        code="print('work')",
        network_policy=SandboxNetworkPolicy.unrestricted(),
    )

    with pytest.raises(SandboxPolicyUnsupportedError, match="retain"):
        provider.execute(request)
    backend.execute.assert_not_called()
