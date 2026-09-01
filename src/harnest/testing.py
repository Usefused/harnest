"""Pytest integration for import-free authored agent tests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

import pytest

from .bundle import (
    EvalSuite,
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
from .logging import get_logger


class AgentTestError(RuntimeError):
    """An authored test suite does not follow the Harnest convention."""


_EVAL_AUDIT = get_logger("eval.audit")


def _stdlib_loggers() -> list[logging.Logger]:
    values = [logging.getLogger()]
    values.extend(
        value
        for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    )
    return values


def _restore_eval_logger(
    logger: logging.Logger,
    settings: tuple[tuple[logging.Handler, ...], int, bool, bool] | None,
) -> None:
    retained = () if settings is None else settings[0]
    for handler in tuple(logger.handlers):
        if handler in retained:
            continue
        logger.removeHandler(handler)
        handler.close()
    if settings is not None:
        _, logger.level, logger.propagate, logger.disabled = settings


def _remove_closed_stream_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        stream = getattr(handler, "stream", None)
        if not bool(getattr(stream, "closed", False)):
            continue
        # In-process pytest can leave its capture handler attached after closing
        # the stream. Keeping it makes later ADK/plugin logs raise during eval.
        logger.removeHandler(handler)
        handler.close()


@contextmanager
def _isolated_eval_logging() -> Iterator[None]:
    """Remove handlers added by ADK eval dependencies when their run ends."""

    loggers = _stdlib_loggers()
    for logger in loggers:
        _remove_closed_stream_handlers(logger)
    original = {
        logger: (
            tuple(logger.handlers),
            logger.level,
            logger.propagate,
            logger.disabled,
        )
        for logger in loggers
    }
    try:
        yield
    finally:
        for logger in _stdlib_loggers():
            _restore_eval_logger(logger, original.get(logger))


def _eval_output_filter_plugin() -> Any:
    """Build an eval-only ADK plugin without importing ADK during test discovery."""

    from .evaluation import customer_facing_eval_output_plugin

    return customer_facing_eval_output_plugin()


@contextmanager
def _adk_eval_output_filter(module_name: str) -> Iterator[None]:
    """Temporarily prepend customer-facing normalization to an ADK eval App."""

    agent_module = importlib.import_module(module_name)
    app = getattr(agent_module, "app", None)
    plugins = getattr(app, "plugins", None)
    if not isinstance(plugins, list):
        raise AgentTestError("compiled ADK eval application has no plugin list")
    plugin = _eval_output_filter_plugin()
    plugins.insert(0, plugin)
    try:
        yield
    finally:
        plugins.remove(plugin)


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


def _eval_config(suite: EvalSuite, trajectory: str) -> Any:
    """Apply a named trajectory policy without replacing authored thresholds."""

    from .evaluation import eval_config

    return eval_config(suite, trajectory)


def _require_eval_trajectory(trajectory: str) -> None:
    from .evaluation import EvaluationError, require_eval_trajectory

    try:
        require_eval_trajectory(trajectory)
    except EvaluationError as exc:
        raise AgentTestError(str(exc)) from exc


def _run_adk_evals(
    artifact: Path,
    suite: EvalSuite,
    *,
    trajectory: str = "business",
    print_results: bool = True,
) -> int:
    """Run validated eval sets through ADK's official evaluator."""

    AgentEvaluator, EvalSet = _eval_dependencies()
    config = _eval_config(suite, trajectory)
    module_name = f"{artifact.name}.agent"

    async def evaluate_all() -> None:
        await _evaluate_eval_sets(
            AgentEvaluator,
            EvalSet,
            module_name=module_name,
            suite=suite,
            config=config,
            trajectory=trajectory,
            print_results=print_results,
        )

    import_root = str(artifact.parent)
    sys.path.insert(0, import_root)
    try:
        with _isolated_eval_logging(), _adk_eval_output_filter(module_name):
            try:
                asyncio.run(evaluate_all())
            except AssertionError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0
    finally:
        if sys.path and sys.path[0] == import_root:
            sys.path.pop(0)
        else:  # pragma: no cover - defensive against third-party sys.path edits
            try:
                sys.path.remove(import_root)
            except ValueError:
                pass
        for name in tuple(sys.modules):
            if name == artifact.name or name.startswith(f"{artifact.name}."):
                sys.modules.pop(name, None)


def _run_langgraph_evals(
    application: Any,
    suite: EvalSuite,
    *,
    trajectory: str = "business",
    print_results: bool = True,
) -> int:
    """Run LangGraph through the same ADK metric registry and eval-set runner."""

    AgentEvaluator, EvalSet = _eval_dependencies()
    config = _eval_config(suite, trajectory)
    if config.live_model_config is not None:
        raise AgentTestError(
            "LangGraph --evals does not support ADK liveModelConfig; use text "
            "conversation evals or authored smoke tests for bidirectional media"
        )

    async def evaluate_all() -> None:
        from .eval_langgraph import langgraph_eval_agent_module

        async with langgraph_eval_agent_module(application) as module_name:
            await _evaluate_eval_sets(
                AgentEvaluator,
                EvalSet,
                module_name=module_name,
                suite=suite,
                config=config,
                trajectory=trajectory,
                print_results=print_results,
            )

    with _isolated_eval_logging():
        try:
            asyncio.run(evaluate_all())
        except AssertionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def _eval_dependencies() -> tuple[Any, Any]:
    """Load the single evaluator dependency used by both framework adapters."""

    from .evaluation import EvaluationError, eval_dependencies

    try:
        return eval_dependencies()
    except EvaluationError as exc:
        raise AgentTestError(str(exc)) from exc


async def _evaluate_eval_sets(
    evaluator: Any,
    eval_set_class: Any,
    *,
    module_name: str,
    suite: EvalSuite,
    config: Any,
    trajectory: str,
    print_results: bool = True,
) -> None:
    """Evaluate every validated suite with consistent output and audit events."""

    for path in suite.eval_sets:
        eval_set = eval_set_class.model_validate_json(path.read_text(encoding="utf-8"))
        print(f"harnest eval [{trajectory}]: {path.name}")
        _EVAL_AUDIT.info(
            "eval.started", trigger="user", outcome="started", suite=path.name
        )
        try:
            await evaluator.evaluate_eval_set(
                agent_module=module_name,
                eval_set=eval_set,
                eval_config=config,
                num_runs=1,
                print_detailed_results=print_results,
            )
        except BaseException:
            _EVAL_AUDIT.info(
                "eval.finished",
                trigger="user",
                outcome="failed",
                suite=path.name,
            )
            raise
        _EVAL_AUDIT.info(
            "eval.finished", trigger="user", outcome="completed", suite=path.name
        )


def run_agent_tests(
    source: str | Path,
    *,
    include_smoke: bool = False,
    include_evals: bool = False,
    eval_trajectory: str = "business",
    no_output: bool = False,
    framework: str = "adk",
    mode: str = "managed",
    cli_enabled: bool = False,
) -> int:
    """Compile an authored agent and run its convention-based pytest suites."""

    _require_eval_trajectory(eval_trajectory)
    source_directory = Path(source)
    if source_directory.is_file():
        source_directory = source_directory.parent
    source_directory = source_directory.resolve()

    with tempfile.TemporaryDirectory(prefix="harnest-test-") as temp_directory:
        artifact = Path(temp_directory) / "compiled_agent"
        compile_artifact(
            source_directory,
            artifact,
            framework=framework,
            mode=mode,
            cli_enabled=cli_enabled,
        )
        try:
            selected = _selected_test_directories(artifact, include_smoke)
            authored = _authored_test_directories(selected)
            plugin = _HarnestPytestPlugin(artifact, include_smoke=include_smoke)
            with _test_output(suppressed=no_output):
                test_status = _run_pytest(plugin, authored)
            if test_status != 0 or not include_evals:
                return test_status

            suite = discover_evals(artifact / "source" / "agent.py")
            if not suite.eval_sets:
                raise AgentTestError(
                    "--evals requires evals/ with at least one *.evalset.json"
                )
            if framework == "adk":
                with _test_output(suppressed=no_output):
                    return _run_adk_evals(
                        artifact,
                        suite,
                        trajectory=eval_trajectory,
                        print_results=not no_output,
                    )
            with _test_output(suppressed=no_output):
                return _run_langgraph_evals(
                    plugin.compiled_application,
                    suite,
                    trajectory=eval_trajectory,
                    print_results=not no_output,
                )
        finally:
            release_authored_library(artifact / "source")


def _selected_test_directories(artifact: Path, include_smoke: bool) -> list[Path]:
    tests = artifact / "source" / "tests"
    selected = [tests / "unit"]
    if include_smoke:
        selected.append(tests / "smoke")
    missing = [path for path in selected if not path.is_dir()]
    if missing:
        expected = ", ".join(
            str(path.relative_to(artifact / "source")) for path in missing
        )
        raise AgentTestError(f"missing authored test directory: {expected}")
    return selected


def _authored_test_directories(selected: list[Path]) -> list[Path]:
    """Skip placeholder-only suites while keeping missing folders actionable."""

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


__all__ = ["AgentTestError", "SmokeClient", "run_agent_tests"]
