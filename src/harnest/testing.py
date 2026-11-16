"""Pytest integration for import-free authored agent tests."""

from __future__ import annotations

from contextlib import (
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
import json
import os
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

import pytest

from .frameworks.registry import DEFAULT_FRAMEWORK
from .testing_storage import TaskStoreConformanceMixin
from .testing_memory import MemoryStoreConformanceMixin
from .testing_channels import ChannelStoreConformanceMixin
from .bundle import (
    _discover_tools,
    _tool_name,
    compile_artifact,
    discover_evals,
)
from .runtime import (
    create_fastapi_app,
    load_compiled_application,
)
from ._library import release_authored_library


class AgentTestError(RuntimeError):
    """An authored test suite does not follow the Harnest convention."""


_EVAL_RESULT_API_VERSION = "harnest.dev/v1alpha1"


def _write_eval_result(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace the explicitly selected JSON result file."""

    destination = path.expanduser().resolve()
    if destination.exists() and destination.is_dir():
        raise AgentTestError(f"eval output path is a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # A unique file in the destination directory prevents concurrent runs
        # from clobbering each other and keeps the final replace atomic.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise AgentTestError(
            f"could not write eval output {destination}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SmokeClient:
    """Small neutral-API helper injected into opted-in smoke tests."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def respond(
        self,
        input: str,
        *,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke the neutral response endpoint and return its JSON object."""

        payload: dict[str, Any] = {"input": input}
        if session_id is not None:
            payload["sessionId"] = session_id
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        response = self._client.post("/responses", json=payload)
        if response.status_code != 200:
            raise AgentTestError(
                f"POST /responses returned {response.status_code}: {response.text}"
            )
        value = response.json()
        if not isinstance(value, dict):
            raise AgentTestError("POST /responses returned a non-object JSON body")
        return value

    def stream(
        self,
        input: str,
        *,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Invoke the streaming endpoint and return parsed server-sent events."""

        payload: dict[str, Any] = {"input": input, "stream": True}
        if session_id is not None:
            payload["sessionId"] = session_id
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        response = self._client.post("/responses", json=payload)
        if response.status_code != 200:
            raise AgentTestError(
                f"streaming POST /responses returned {response.status_code}: "
                f"{response.text}"
            )
        return _parse_sse(response.text)


def _parse_sse(payload: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_name: str | None = None
    data_lines: list[str] = []

    def finish() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = None
            return
        try:
            value = json.loads("\n".join(data_lines))
        except ValueError as exc:
            raise AgentTestError(f"stream returned invalid SSE JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise AgentTestError("stream returned a non-object SSE data payload")
        if event_name is not None and "type" not in value:
            value["type"] = event_name
        events.append(value)
        event_name = None
        data_lines = []

    for line in payload.splitlines():
        if not line:
            finish()
        elif line.startswith("event:"):
            event_name = line.partition(":")[2].lstrip()
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].lstrip())
    finish()
    return events


class _HarnestPytestPlugin:
    def __init__(self, artifact: Path, *, include_smoke: bool) -> None:
        self.artifact = artifact
        self.include_smoke = include_smoke
        self.compiled_application = load_compiled_application(artifact)
        self.compiled_agent = self.compiled_application.target
        source = artifact / "source"
        discovered = list(_discover_tools(source / "tools"))
        self.discovered_tools = MappingProxyType(
            {_tool_name(tool): tool for tool in discovered}
        )

    @staticmethod
    def _is_smoke(request: Any) -> bool:
        path = Path(str(request.node.path))
        return "smoke" in path.parts

    def _require_smoke(self, request: Any, fixture_name: str) -> None:
        if not self.include_smoke or not self._is_smoke(request):
            import pytest

            pytest.fail(
                f"the {fixture_name} fixture is available only in "
                "tests/smoke with 'harnest test AGENT_DIR --smoke'",
                pytrace=False,
            )

    @contextmanager
    def _isolated_smoke_client(
        self, request: Any, fixture_name: str
    ) -> Iterator[Any]:
        """Expose shared server ownership without sharing per-test HTTP state."""

        # Resolve the session fixture only after validating the test location;
        # otherwise an invalid unit test could start user-owned resources.
        self._require_smoke(request, fixture_name)
        client = request.getfixturevalue("_harnest_smoke_client")
        headers = client.headers.copy()
        cookies = type(client.cookies)(client.cookies)
        try:
            yield client
        finally:
            # The application lifespan is suite-scoped, but request defaults are
            # test-owned so authentication state cannot influence the next test.
            client.headers = headers
            client.cookies = cookies

    def pytest_configure(self, config: Any) -> None:
        config.addinivalue_line(
            "markers",
            "harnest_smoke: an authored agent smoke test selected by --smoke",
        )

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        import pytest

        for item in items:
            if "smoke" in Path(str(item.path)).parts:
                item.add_marker(pytest.mark.harnest_smoke)

    @property
    def pytest_fixtures(self) -> tuple[str, ...]:
        return ("agent", "tools", "client", "smoke")

    @pytest.fixture(scope="session")
    def agent(self) -> Any:
        return self.compiled_agent

    @pytest.fixture(scope="session")
    def tools(self) -> Mapping[str, Any]:
        return self.discovered_tools

    @pytest.fixture(scope="session")
    def _harnest_smoke_client(self) -> Iterator[Any]:
        """Keep one server lifecycle around every selected smoke test."""

        from fastapi.testclient import TestClient

        app = create_fastapi_app(self.artifact, bind_host="testserver")
        # The compiled application owns shared lifecycle resources. Closing an
        # app per test would close those resources while later tests still use it.
        with TestClient(app) as test_client:
            yield test_client

    @pytest.fixture
    def client(self, request: Any) -> Iterator[Any]:
        with self._isolated_smoke_client(request, "client") as test_client:
            yield test_client

    @pytest.fixture
    def smoke(self, request: Any) -> Iterator[SmokeClient]:
        with self._isolated_smoke_client(request, "smoke") as test_client:
            yield SmokeClient(test_client)


def _require_eval_trajectory(trajectory: str) -> None:
    from .evaluation import EvaluationError, require_eval_trajectory

    try:
        require_eval_trajectory(trajectory)
    except EvaluationError as exc:
        raise AgentTestError(str(exc)) from exc


def _eval_progress(message: str, *, enabled: bool) -> None:
    """Flush human progress to stderr without mixing it into structured results."""

    if enabled:
        print(f"harnest eval: {message}", file=sys.stderr, flush=True)


def _selected_evaluations(
    artifact: Path, project: Path, plugin: _HarnestPytestPlugin, framework: str,
    trajectory: str, quiet: bool, output: Path | None,
) -> int:
    """Keep the legacy report for ADK alone and compose reports for installed runners."""
    from ._eval_runner import run_configured
    from ._eval_runner_config import BUILTIN_RUNNER, read_runners

    runners = read_runners(artifact / "source")

    def run_builtin(destination: Path | None) -> int:
        """Discover eval files only when the built-in evaluator is selected."""
        suite = discover_evals(artifact / "source" / "agent.py", framework=framework)
        if not suite.eval_sets:
            raise AgentTestError("--evals requires evals/ with at least one *.evalset.json")
        from .frameworks.registry import get_framework

        return get_framework(framework).run_builtin_evals(
            artifact=artifact, application=plugin.compiled_application, suite=suite,
            trajectory=trajectory, print_results=not quiet, result_output=destination,
        )

    with _test_output(suppressed=quiet):
        if [runner.name for runner in runners] == [BUILTIN_RUNNER]:
            return run_builtin(output)
        return run_configured(runners, artifact, project, output, run_builtin, _write_eval_result, quiet=quiet)


def run_agent_tests(
    source: str | Path,
    *,
    include_smoke: bool = False,
    include_evals: bool = False,
    eval_trajectory: str = "business",
    no_output: bool = False,
    eval_output: Path | None = None,
    framework: str = DEFAULT_FRAMEWORK,
    mode: str = "managed",
    cli_enabled: bool = False,
) -> int:
    """Test authored source with eval preparation progress and optional quiet output."""

    _require_eval_trajectory(eval_trajectory)
    # Reject a dead output target before compiling so direct Python callers get
    # the same contract as the public Go command.
    if eval_output is not None and not include_evals:
        raise AgentTestError("--eval-output requires --evals")
    show_progress = include_evals and not no_output
    source_directory = Path(source)
    if source_directory.is_file():
        source_directory = source_directory.parent
    source_directory = source_directory.resolve()

    with tempfile.TemporaryDirectory(prefix="harnest-test-") as temp_directory:
        artifact = Path(temp_directory) / "compiled_agent"
        _eval_progress("Compiling agent", enabled=show_progress)
        compile_artifact(
            source_directory,
            artifact,
            framework=framework,
            mode=mode,
            cli_enabled=cli_enabled,
            bundle_selection=False,
        )
        try:
            _eval_progress("Loading agent and Python tests", enabled=show_progress)
            selected = _selected_test_directories(artifact, include_smoke)
            authored = _authored_test_directories(selected)
            plugin = _HarnestPytestPlugin(artifact, include_smoke=include_smoke)
            with _test_output(suppressed=no_output):
                _eval_progress("Running Python tests", enabled=show_progress)
                test_status = _run_pytest(plugin, authored)
            if test_status != 0 or not include_evals:
                return test_status

            return _selected_evaluations(artifact, source_directory, plugin, framework,
                                         eval_trajectory, no_output, eval_output)
        finally:
            release_authored_library(artifact / "source")


def _selected_test_directories(artifact: Path, include_smoke: bool) -> list[Path]:
    """Select present optional suites and reject paths that block a suite."""

    tests = artifact / "source" / "tests"
    selected = [tests / "unit"]
    if include_smoke:
        selected.append(tests / "smoke")
    invalid = [path for path in selected if path.exists() and not path.is_dir()]
    if invalid:
        expected = ", ".join(
            str(path.relative_to(artifact / "source")) for path in invalid
        )
        raise AgentTestError(f"authored test path must be a directory: {expected}")
    # Test suites are optional capabilities, so deleting an unused folder must
    # behave exactly like keeping its ignored guide without a public test file.
    return [path for path in selected if path.is_dir()]


def _authored_test_directories(selected: list[Path]) -> list[Path]:
    """Skip placeholder-only suites while keeping public tests actionable."""

    return [
        path
        for path in selected
        if any(candidate.is_file() for candidate in path.glob("test_*.py"))
    ]


@contextmanager
def _test_output(*, suppressed: bool) -> Iterator[None]:
    """Suppress authored test streams without changing execution or exit status."""

    if not suppressed:
        yield
        return
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


def _run_pytest(plugin: Any, selected: list[Path]) -> int:
    if not selected:
        print("harnest test: no authored Python tests")
        return 0
    return int(pytest.main(_pytest_args(selected), plugins=[plugin]))


def _pytest_args(selected: list[Path]) -> list[str]:
    return [
        *(str(path) for path in selected),
        "-q", "--disable-warnings", "-W",
        "ignore::pytest.PytestAssertRewriteWarning",
        "-p", "no:cacheprovider", "-p", "no:anyio",
    ]


__all__ = ["AgentTestError", "SmokeClient", "TaskStoreConformanceMixin", "MemoryStoreConformanceMixin", "ChannelStoreConformanceMixin", "run_agent_tests"]
