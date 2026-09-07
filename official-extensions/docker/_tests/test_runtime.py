"""Exercise Docker startup ownership and execution without a daemon."""

from __future__ import annotations

import io
import struct
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from harnest.sandbox import SandboxContext, SandboxRequest, SandboxStatus, control
from harnest_extension_docker.lib.docker_runtime import (
    DockerStartupError,
    create_docker_executor,
)
from harnest_extension_docker.lib.guard import (
    SandboxCleanupError,
    close_guarded_executor,
    guard_failed,
)


def _frame(channel: int, value: bytes) -> bytes:
    """Encode one Docker non-TTY output frame."""

    return struct.pack(">BxxxL", channel, len(value)) + value


def _container(raw: object) -> Mock:
    """Expose the Docker methods used during startup, execution, and cleanup."""

    api = Mock(timeout=60)
    api.exec_create.return_value = {"Id": "owned-exec"}
    api.exec_start.side_effect = lambda *_args, **_kwargs: raw
    api.exec_inspect.return_value = {"ExitCode": 0}
    native = Mock(id="owned-container", client=SimpleNamespace(api=api))
    native.attrs = {}
    return native


def _client(native: Mock, *, image: Mock | None = None) -> Mock:
    """Build a client whose collection allocates one exact owned container."""

    client = Mock()
    client.containers.client = client
    client.networks.client = client
    client.containers.create.return_value = native
    client.images.get.return_value = image or Mock(
        id="sha256:image", attrs={"Config": {}}
    )
    return client


def _topology(service: dict[str, object] | None = None) -> dict[str, object]:
    """Provide one complete internal service topology runtime snapshot."""

    defaults: dict[str, object] = {
        "name": "api",
        "image": "service@sha256:test",
        "docker_path": None,
        "command": ["serve", "--port", "8080"],
        "environment": {"MODE": "test"},
        "ports": [8080],
        "aliases": ["backend"],
        "readiness": {
            "command": ["ready"],
            "interval_seconds": 1.0,
            "timeout_seconds": 2.0,
            "retries": 3,
            "start_period_seconds": 0.5,
        },
        "harnest_resource_limits": {
            "read_only": True,
            "pids_limit": 8,
        },
    }
    defaults.update(service or {})
    return {"network": {"internal": True}, "services": [defaults]}


def _config(**overrides: object) -> dict[str, object]:
    """Provide a complete validated backend configuration snapshot."""

    config: dict[str, object] = {
        "image": "python:3.12",
        "docker_path": None,
        "base_url": None,
        "network_enabled": False,
        "timeout_seconds": 10,
        "harnest_resource_limits": {
            "read_only": True,
            "pids_limit": 64,
        },
    }
    config.update(overrides)
    return config


def _assert_service_options(options: dict[str, object]) -> None:
    """Verify topology service policy without inflating its lifecycle test."""

    assert options["command"] == ["serve", "--port", "8080"]
    assert options["environment"] == {"MODE": "test"}
    assert "ports" not in options
    assert options["pids_limit"] == 8
    assert "_harnest_limits" not in options
    assert options["healthcheck"] == {
        "test": ["CMD", "ready"],
        "interval": 1_000_000_000,
        "timeout": 2_000_000_000,
        "retries": 3,
        "start_period": 500_000_000,
    }


def _assert_topology_connections(client: Mock, network: Mock) -> None:
    """Verify internal DNS attachment without host-published ports."""

    service_options = client.containers.create.call_args_list[0].kwargs
    primary_options = client.containers.create.call_args_list[1].kwargs
    assert service_options["network"] == network.name
    assert primary_options["network"] == network.name
    assert client.api.create_endpoint_config.call_args_list[0].kwargs == {
        "aliases": ["api", "backend"]
    }
    assert client.api.create_endpoint_config.call_args_list[1].kwargs == {
        "aliases": ["sandbox"]
    }
    assert client.networks.create.call_args.kwargs["internal"] is True


def test_startup_applies_limits_and_network_before_probe() -> None:
    """The first container is isolated and budgeted before image code executes."""

    native = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(native)
    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(_config(), 1024)
    try:
        kwargs = client.containers.create.call_args.kwargs
        assert kwargs["network_mode"] == "none"
        assert kwargs["read_only"] is True
        assert kwargs["pids_limit"] == 64
        assert kwargs["entrypoint"][:2] == ["python3", "-c"]
        assert kwargs["healthcheck"] == {"test": ["NONE"]}
        assert kwargs["labels"] == {
            "dev.harnest.managed": "true",
            "dev.harnest.resource": "sandbox",
        }
        assert native.client.api.exec_create.call_args.args[1] == [
            "python3",
            "--version",
        ]
    finally:
        close_guarded_executor(executor)
    native.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_called_once_with()


def test_custom_daemon_and_unrestricted_network_are_explicit() -> None:
    """Only authored base URL and network authority alter Docker startup."""

    native = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(native)
    with patch("docker.DockerClient", return_value=client) as factory:
        executor = create_docker_executor(
            _config(base_url="unix:///owned.sock", network_enabled=True), 1024
        )
    try:
        factory.assert_called_once_with(
            base_url="unix:///owned.sock", timeout=10
        )
        assert client.containers.create.call_args.kwargs["network_mode"] == "bridge"
    finally:
        executor.close()


def test_startup_transport_uses_remaining_sandbox_deadline() -> None:
    """A Docker API call cannot retain the SDK's longer default timeout."""

    native = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(native)
    with (
        control.execute(2),
        patch("docker.from_env", return_value=client) as factory,
    ):
        executor = create_docker_executor(_config(timeout_seconds=30), 1024)
    try:
        initial_timeout = factory.call_args.kwargs["timeout"]
        assert 0 < initial_timeout <= 2
        assert 0 < client.api.timeout <= initial_timeout
    finally:
        executor.close()


def test_image_volume_is_rejected_before_container_creation() -> None:
    """Dockerfile volumes cannot bypass the single budgeted writable scratch."""

    native = _container(io.BytesIO())
    image = Mock(id="sha256:unsafe", attrs={"Config": {"Volumes": {"/data": {}}}})
    client = _client(native, image=image)
    with patch("docker.from_env", return_value=client):
        with pytest.raises(ValueError, match="must not declare volumes"):
            create_docker_executor(_config(), 1024)
    client.containers.create.assert_not_called()
    client.close.assert_called_once_with()


def test_dockerfile_build_uses_exact_image_id() -> None:
    """A local build cannot race through a shared mutable image tag."""

    native = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(native)
    built = Mock(id="sha256:built", attrs={"Config": {}})
    client.images.build.return_value = (built, ["private logs"])
    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(
            _config(image=None, docker_path="sandbox/image"), 1024
        )
    try:
        client.images.build.assert_called_once_with(path="sandbox/image", rm=True)
        assert client.containers.create.call_args.kwargs["image"] == "sha256:built"
    finally:
        executor.close()


def test_unknown_create_outcome_retains_poisoned_owner() -> None:
    """A lost allocation response forbids automatic container replacement."""

    native = _container(io.BytesIO())
    client = _client(native)
    client.containers.create.side_effect = OSError("create response lost")
    with patch("docker.from_env", return_value=client):
        with pytest.raises(DockerStartupError) as caught:
            create_docker_executor(_config(), 1024)
    executor = caught.value.failed_executor
    assert caught.value.phase == "container creation and start"
    assert "OSError" in str(caught.value)
    assert "create response lost" not in str(caught.value)
    assert "cleanup could not be confirmed" in str(caught.value)
    assert guard_failed(executor)
    assert executor._container is None
    with pytest.raises(SandboxCleanupError):
        close_guarded_executor(executor)
    client.close.assert_called_once_with()


def test_start_failure_cleans_the_allocated_container() -> None:
    """Ownership captured before start allows deterministic failure cleanup."""

    native = _container(io.BytesIO())
    native.start.side_effect = RuntimeError("image start failed")
    client = _client(native)
    with patch("docker.from_env", return_value=client):
        with pytest.raises(
            DockerStartupError, match="container creation and start"
        ) as caught:
            create_docker_executor(_config(), 1024)
    assert "image start failed" not in str(caught.value)
    native.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_called_once_with()


def test_client_failure_reports_sanitized_startup_phase() -> None:
    """Name daemon initialization without retaining its private SDK message."""

    with patch(
        "docker.from_env", side_effect=OSError("private daemon endpoint")
    ):
        with pytest.raises(
            DockerStartupError, match="daemon client initialization"
        ) as caught:
            create_docker_executor(_config(), 1024)
    assert caught.value.cause_type == "OSError"
    assert "private daemon endpoint" not in str(caught.value)


def test_probe_timeout_is_not_reported_as_missing_python() -> None:
    """Preserve a host timeout as the Python startup-probe diagnostic."""

    native = _container(io.BytesIO())
    client = _client(native)

    def timed_out(guard, *_args, **_kwargs):
        """Return the same terminal state produced by host deadline enforcement."""

        guard.status = SandboxStatus.TIMED_OUT
        return SimpleNamespace(exit_code=124)

    with (
        patch("docker.from_env", return_value=client),
        patch(
            "harnest_extension_docker.lib.docker_runtime.GuardedContainer.exec_run",
            autospec=True,
            side_effect=timed_out,
        ),
    ):
        with pytest.raises(TimeoutError, match="Python availability probe") as caught:
            create_docker_executor(_config(), 1024)
    assert "requires python3" not in str(caught.value)
    native.remove.assert_called_once_with(force=True, v=True)


def test_expired_create_response_cannot_start_container() -> None:
    """Authority is rechecked after allocation and before starting image code."""

    native = _container(io.BytesIO())
    client = _client(native)
    with control.execute(None) as current:

        def revoked_create(*_args: object, **_kwargs: object) -> Mock:
            """Expire authority while Docker allocates the resource."""

            current.cancelled.set()
            return native

        client.containers.create.side_effect = revoked_create
        with patch("docker.from_env", return_value=client):
            with pytest.raises(RuntimeError, match="cancelled"):
                create_docker_executor(_config(), 1024)
    native.start.assert_not_called()
    native.remove.assert_called_once_with(force=True, v=True)


def test_execute_preserves_output_and_nonzero_status() -> None:
    """Provider results reflect process exit status rather than stderr presence."""

    native = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(native)
    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(_config(), 1024)
    try:
        output = io.BytesIO(_frame(1, b"work\n") + _frame(2, b"warning\n"))
        native.client.api.exec_start.side_effect = (
            lambda *_args, **_kwargs: output
        )
        native.client.api.exec_inspect.return_value = {"ExitCode": 7}
        result = executor.execute(
            SandboxRequest(
                "raise SystemExit(7)",
                context=SandboxContext("agent", "turn", "user", "session"),
            )
        )
        assert result.stdout == "work\n"
        assert result.stderr == "warning\n"
        assert (result.status, result.exit_code) == (SandboxStatus.FAILED, 7)
    finally:
        executor.close()


def test_topology_starts_services_before_primary_and_cleans_in_reverse() -> None:
    """Own the bridge, readiness, aliases, limits, and complete cleanup ordering."""

    service = _container(io.BytesIO())
    service.id = "service-container"
    service.attrs = {
        "State": {"Status": "running", "Health": {"Status": "healthy"}}
    }
    primary = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(primary)
    client.containers.create.side_effect = [service, primary]
    network = Mock(client=client)
    network.name = "harnest-test-network"
    client.networks.create.return_value = network
    cleanup: list[str] = []
    primary.remove.side_effect = lambda **_kwargs: cleanup.append("primary")
    service.remove.side_effect = lambda **_kwargs: cleanup.append("service")
    network.remove.side_effect = lambda: cleanup.append("network")

    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(
            _config(topology=_topology(), network_enabled=False), 1024
        )

    _assert_service_options(client.containers.create.call_args_list[0].kwargs)
    _assert_topology_connections(client, network)

    executor.close()

    assert cleanup == ["primary", "service", "network"]
    client.close.assert_called_once_with()


def test_retained_topology_fails_closed_when_a_service_stops() -> None:
    """Never run new code against a retained topology that lost a service."""

    service = _container(io.BytesIO())
    service.attrs = {"State": {"Status": "running"}}
    primary = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(primary)
    client.containers.create.side_effect = [service, primary]
    network = Mock(client=client)
    network.name = "harnest-test-network"
    client.networks.create.return_value = network
    config = _config(
        topology=_topology({"readiness": None}),
        network_enabled=False,
    )
    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(config, 1024)
    service.attrs = {"State": {"Status": "exited"}}

    with pytest.raises(RuntimeError, match="service 'api' is unavailable"):
        executor.execute(SandboxRequest("print('must not run')"))

    assert guard_failed(executor)
    assert primary.client.api.exec_create.call_count == 1
    executor.close()


def test_unknown_network_create_outcome_retains_poisoned_owner() -> None:
    """A lost network response blocks replacement without leaking SDK details."""

    client = _client(_container(io.BytesIO()))
    client.networks.create.side_effect = OSError("private network response")
    with patch("docker.from_env", return_value=client):
        with pytest.raises(DockerStartupError) as caught:
            create_docker_executor(_config(topology=_topology()), 1024)

    executor = caught.value.failed_executor
    assert caught.value.phase == "topology network creation"
    assert "OSError" in str(caught.value)
    assert "private network response" not in str(caught.value)
    with pytest.raises(SandboxCleanupError):
        close_guarded_executor(executor)
    client.close.assert_called_once_with()


def test_service_start_failure_removes_container_then_network() -> None:
    """A deterministic service failure unwinds every earlier topology resource."""

    service = _container(io.BytesIO())
    service.start.side_effect = RuntimeError("private service failure")
    client = _client(service)
    network = Mock(client=client)
    network.name = "harnest-test-network"
    client.networks.create.return_value = network
    cleanup: list[str] = []
    service.remove.side_effect = lambda **_kwargs: cleanup.append("service")
    network.remove.side_effect = lambda: cleanup.append("network")

    with patch("docker.from_env", return_value=client):
        with pytest.raises(DockerStartupError) as caught:
            create_docker_executor(_config(topology=_topology()), 1024)

    assert caught.value.phase == "service 'api' container creation and start"
    assert "RuntimeError" in str(caught.value)
    assert "private service failure" not in str(caught.value)
    assert cleanup == ["service", "network"]
    client.close.assert_called_once_with()


def test_network_cleanup_failure_retains_only_the_failed_resource() -> None:
    """Retry exact network cleanup after every owned container is gone."""

    service = _container(io.BytesIO())
    service.attrs = {"State": {"Status": "running"}}
    primary = _container(io.BytesIO(_frame(1, b"Python 3.12\n")))
    client = _client(primary)
    client.containers.create.side_effect = [service, primary]
    network = Mock(client=client)
    network.name = "harnest-test-network"
    network.remove.side_effect = [OSError("daemon unavailable"), None]
    client.networks.create.return_value = network
    with patch("docker.from_env", return_value=client):
        executor = create_docker_executor(
            _config(topology=_topology({"readiness": None})), 1024
        )

    with pytest.raises(SandboxCleanupError):
        executor.close()
    assert executor._owned_containers == []
    assert executor._owned_networks == [network]
    client.close.assert_not_called()

    executor.close()

    assert executor._owned_networks == []
    client.close.assert_called_once_with()
