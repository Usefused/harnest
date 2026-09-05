"""Exercise Docker scope ownership and hard policy without a daemon."""

from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from harnest.sandbox import (
    SandboxBudget,
    SandboxContext,
    SandboxInputFilesUnsupportedError,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
)
from harnest_extension_docker.lib.backend import (
    create_docker_backend,
    validate_adapter_options,
)


def _executor() -> SimpleNamespace:
    """Represent one owned container with an inspectable execution method."""

    return SimpleNamespace(
        _guard=None,
        _guard_poisoned=False,
        execute=Mock(return_value=SandboxResult(stdout="done", exit_code=0)),
    )


def _request(
    user: str = "alice",
    session: str = "first",
    agent: str = "worker",
    invocation: str = "turn-1",
) -> SandboxRequest:
    """Make scope-key collisions visible by varying each identity component."""

    return SandboxRequest(
        "print(1)",
        context=SandboxContext(agent, invocation, user, session),
    )


def test_default_scope_destroys_every_container() -> None:
    """Fresh execution scopes cannot carry files into a later call."""

    owners = [_executor() for _ in range(3)]
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        side_effect=owners,
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor"
    ) as close:
        backend = create_docker_backend(image="python")
        for _ in owners:
            assert backend.execute(_request()).status == SandboxStatus.SUCCEEDED
    assert create.call_count == 3
    assert [call.args[0] for call in close.call_args_list] == owners


def test_session_scope_never_crosses_identity() -> None:
    """A session label alone cannot authorize another tenant's container."""

    owners = [_executor() for _ in range(4)]
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        side_effect=owners,
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor"
    ) as close:
        backend = create_docker_backend(image="python", scope="session")
        for value in (
            _request(),
            _request(),
            _request(user="bob"),
            _request(session="second"),
            _request(agent="child"),
        ):
            backend.execute(value)
        backend.close()
    assert create.call_count == 4
    assert owners[0].execute.call_count == 2
    assert close.call_count == 4


def test_invocation_scope_requires_complete_identity() -> None:
    """Invocation retention includes turn identity and rejects anonymous calls."""

    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        side_effect=lambda *_: _executor(),
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor"
    ):
        backend = create_docker_backend(image="python", scope="invocation")
        backend.execute(_request())
        backend.execute(_request(invocation="turn-2"))
        with pytest.raises(ValueError, match="identity"):
            backend.execute(SandboxRequest("print(1)"))
    assert create.call_count == 2


def test_retained_budget_evicts_only_after_cleanup() -> None:
    """An uncertain LRU cleanup blocks replacement without losing ownership."""

    first, second = _executor(), _executor()
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        side_effect=[first, second],
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor",
        side_effect=[RuntimeError("cleanup"), None],
    ):
        backend = create_docker_backend(
            image="python", scope="session", max_scopes=1
        )
        backend.execute(_request())
        with pytest.raises(RuntimeError, match="cleanup"):
            backend.execute(_request(user="bob"))
        assert create.call_count == 1
        backend.execute(_request(user="bob"))
    assert create.call_count == 2


def test_cleanup_failure_blocks_provider_replacement() -> None:
    """Default scopes keep a poisoned owner until cleanup can be retried."""

    owner = _executor()
    owner._guard_poisoned = True
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        return_value=owner,
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor",
        side_effect=RuntimeError("cleanup"),
    ):
        backend = create_docker_backend(image="python")
        with pytest.raises(RuntimeError, match="cleanup"):
            backend.execute(_request())
        with pytest.raises(RuntimeError, match="cleanup"):
            backend.execute(_request())
    assert backend._executor is owner
    assert create.call_count == 1


def test_concurrent_fresh_scopes_do_not_share_containers() -> None:
    """Serialized admission still assigns every request a fresh filesystem."""

    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        side_effect=lambda *_: _executor(),
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor"
    ):
        backend = create_docker_backend(image="python")
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(backend.execute, [_request()] * 8))
    assert len(results) == 8
    assert create.call_count == 8


def test_hard_budget_translation_reaches_docker() -> None:
    """Provider-neutral budgets become kernel-enforced Docker settings."""

    budget = SandboxBudget(
        cpu=0.5,
        memory_bytes=64 * 1024 * 1024,
        pids=16,
        scratch_bytes=4096,
    )
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        return_value=_executor(),
    ) as create, patch(
        "harnest_extension_docker.lib.backend.close_guarded_executor"
    ):
        backend = create_docker_backend(
            image="python", budget=budget, max_output_bytes=512
        )
        backend.execute(_request())
    options, limit = create.call_args.args
    limits = options["harnest_resource_limits"]
    assert limit == 512
    assert options["network_enabled"] is False
    assert limits["nano_cpus"] == 500_000_000
    assert limits["mem_limit"] == limits["memswap_limit"]
    assert limits["pids_limit"] == 16
    assert "size=4096" in limits["tmpfs"]["/tmp"]
    assert limits["read_only"] is True
    assert limits["user"] == "65534:65534"


def test_input_files_and_invalid_settings_fail_closed() -> None:
    """Reject unsupported transfer and provider escape settings before Docker."""

    backend = create_docker_backend(image="python")
    request = Mock(spec=SandboxRequest, input_files=(object(),), context=Mock())
    with pytest.raises(SandboxInputFilesUnsupportedError):
        backend.execute(request)
    for values in (
        {"scope": "global"},
        {"max_scopes": 0},
        {"max_output_bytes": 0},
        {"timeout_seconds": None},
    ):
        with pytest.raises(ValueError):
            create_docker_backend(image="python", **values)
    with pytest.raises(ValueError, match="unsupported"):
        validate_adapter_options({"volumes": {"/": "/host"}})
    with pytest.raises(ValueError, match="remain False"):
        validate_adapter_options({"stateful": True})


def test_request_deadline_is_bounded_and_restored() -> None:
    """A shorter request cannot extend or leak into a retained executor."""

    owner = _executor()
    owner.timeout_seconds = 30
    observed: list[int] = []

    def execute(_request: SandboxRequest) -> SandboxResult:
        observed.append(owner.timeout_seconds)
        return SandboxResult()

    owner.execute.side_effect = execute
    with patch(
        "harnest_extension_docker.lib.backend.create_docker_executor",
        return_value=owner,
    ), patch("harnest_extension_docker.lib.backend.close_guarded_executor"):
        backend = create_docker_backend(
            image="python", timeout_seconds=30, scope="session"
        )
        backend.execute(
            SandboxRequest(
                "print(1)", timeout_seconds=2, context=_request().context
            )
        )
        assert owner.timeout_seconds == 30
        backend.execute(_request())
    assert observed == [2, 30]
