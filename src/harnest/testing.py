"""Pytest integration for import-free authored agent tests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
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
from .eval_model_transport import (
    close_owned_eval_model_transports,
    eval_model_transports,
    restore_eval_model_names,
)


class AgentTestError(RuntimeError):
    """An authored test suite does not follow the Harnest convention."""


_EVAL_AUDIT = get_logger("eval.audit")
_EVAL_APP_NAME = "harnest_eval"
_EVAL_RESULT_API_VERSION = "harnest.dev/v1alpha1"


class _EvalResultCollector:
    """Collect complete ADK case results for one Harnest eval command."""

    def __init__(self, *, framework: str, trajectory: str) -> None:
        self._framework = framework
        self._trajectory = trajectory
        self._results: dict[tuple[str, str], dict[str, Any]] = {}

    def register_eval_set(self, *, app_name: str, eval_set_id: str) -> None:
        """Retain an authored suite even when it contains no evaluable cases."""

        self._results.setdefault(
            (app_name, eval_set_id),
            {
                "appName": app_name,
                "evalSetId": eval_set_id,
                "status": "not_evaluated",
                "evalCaseResults": [],
            },
        )

    def save_eval_set_result(
        self,
        app_name: str,
        eval_set_id: str,
        eval_case_results: list[Any],
    ) -> None:
        """Retain one suite in deterministic order through ADK's manager hook."""

        # ADK supplies the logical application owner separately from the suite;
        # retain it so consumers do not need to infer result provenance.
        cases = [
            _eval_case_result_payload(result)
            for result in sorted(eval_case_results, key=lambda item: item.eval_id)
        ]
        self._results[(app_name, eval_set_id)] = {
            "appName": app_name,
            "evalSetId": eval_set_id,
            "status": _aggregate_eval_status(
                case["finalEvalStatus"] for case in cases
            ),
            "evalCaseResults": cases,
        }

    def payload(self, *, status: str) -> dict[str, Any]:
        """Build the stable command-level envelope around collected ADK results."""

        suites = sorted(self._results.values(), key=lambda item: item["evalSetId"])
        cases = [case for suite in suites for case in suite["evalCaseResults"]]
        # Infrastructure failures and scored failures must dominate partial
        # data; only a nominally successful run derives its verdict from cases.
        result_status = (
            _aggregate_eval_status(suite["status"] for suite in suites)
            if status == "passed"
            else status
        )
        return {
            "apiVersion": _EVAL_RESULT_API_VERSION,
            "kind": "EvalRunResult",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "framework": self._framework,
            "trajectory": self._trajectory,
            "status": result_status,
            "summary": {
                "suiteCount": len(suites),
                "caseCount": len(cases),
                "passedCases": sum(
                    case["finalEvalStatus"] == "passed" for case in cases
                ),
                "failedCases": sum(
                    case["finalEvalStatus"] == "failed" for case in cases
                ),
                "notEvaluatedCases": sum(
                    case["finalEvalStatus"] == "not_evaluated" for case in cases
                ),
            },
            "evalSetResults": suites,
        }


def _aggregate_eval_status(statuses: Iterator[str]) -> str:
    """Reduce case statuses without treating missing evaluation as success."""

    values = tuple(statuses)
    # A failed case dominates a mixed suite because the command must remain
    # suitable as a CI quality gate.
    if any(value == "failed" for value in values):
        return "failed"
    # Empty and partially evaluated suites must not be reported as passing.
    if values and all(value == "passed" for value in values):
        return "passed"
    return "not_evaluated"


def _status_name(value: Any) -> str:
    """Expose ADK enum statuses as stable lowercase names instead of ordinals."""

    name = getattr(value, "name", None)
    return str(name).lower() if name is not None else str(value).lower()


def _model_json(value: Any) -> Any:
    """Serialize one ADK model with aliases and provider-safe JSON coercion."""

    if value is None:
        return None
    return restore_eval_model_names(json.loads(value.model_dump_json(by_alias=True)))


def _metric_result_payload(result: Any) -> dict[str, Any]:
    """Serialize a metric result while retaining rubric scores and criteria."""

    payload = _model_json(result)
    payload["evalStatus"] = _status_name(result.eval_status)
    return payload


def _eval_case_result_payload(result: Any) -> dict[str, Any]:
    """Preserve a complete ADK case result with readable enum statuses."""

    payload = _model_json(result)
    payload["finalEvalStatus"] = _status_name(result.final_eval_status)
    payload["overallEvalMetricResults"] = [
        _metric_result_payload(metric)
        for metric in result.overall_eval_metric_results
    ]
    invocations = []
    for invocation in result.eval_metric_result_per_invocation:
        invocation_payload = _model_json(invocation)
        invocation_payload["evalMetricResults"] = [
            _metric_result_payload(metric)
            for metric in invocation.eval_metric_results
        ]
        invocations.append(invocation_payload)
    payload["evalMetricResultPerInvocation"] = invocations
    return payload


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


def _publish_eval_result(
    collector: _EvalResultCollector,
    *,
    status: str,
    print_results: bool,
    result_output: Path | None,
    error: Exception | None = None,
) -> None:
    """Print and optionally persist the same complete eval result payload."""

    payload = collector.payload(status=status)
    if error is not None:
        from .eval_errors import EvaluationExecutionError

        # Provider exceptions can echo API keys, headers, prompts, or payloads.
        # Retain only stable type-level diagnostics in the durable result.
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error) if isinstance(error, EvaluationExecutionError)
            else "evaluation infrastructure failed",
        }
    # Persist first so a broken output stream cannot discard an explicitly
    # requested result artifact after evaluation has already completed.
    if result_output is not None:
        _write_eval_result(result_output, payload)
    if print_results:
        print(json.dumps(payload, indent=2, sort_keys=True))


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


@contextmanager
def _adk_eval_output_filter(module_name: str) -> Iterator[None]:
    """Prepare managed native evaluation without modifying the compiled app."""

    from .evaluation import prepared_adk_eval_app

    agent_module = importlib.import_module(module_name)
    app = getattr(agent_module, "app", None)
    plugins = getattr(app, "plugins", None)
    if not isinstance(plugins, list):
        raise AgentTestError("compiled ADK eval application has no plugin list")
    missing = object()
    original_root = getattr(agent_module, "root_agent", missing)
    with prepared_adk_eval_app(app) as prepared:
        agent_module.app = prepared
        # ADK's evaluator rebuilds App.root_agent from the module export, so
        # both must point at the isolated legacy cancellation-safe root.
        agent_module.root_agent = prepared.root_agent
        try:
            yield
        finally:
            agent_module.app = app
            if original_root is missing:
                del agent_module.root_agent
            else:
                agent_module.root_agent = original_root


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
    result_output: Path | None = None,
) -> int:
    """Run validated eval sets through ADK's official evaluator."""

    AgentEvaluator, EvalSet = _eval_dependencies()
    config = _eval_config(suite, trajectory)
    module_name = f"{artifact.name}.agent"
    collector = _EvalResultCollector(framework="adk", trajectory=trajectory)

    async def evaluate_all() -> None:
        """Keep native evaluation capabilities and model clients in one loop."""

        from .eval_adk import adk_evaluation_runtime
        from .application import CompiledApplication

        target = getattr(sys.modules.get(module_name), "root_agent", None)
        application = getattr(sys.modules.get(module_name), "application", None)
        try:
            async with adk_evaluation_runtime(application):
                with eval_model_transports(target, config) as prepared:
                    await _evaluate_eval_sets(
                        AgentEvaluator,
                        EvalSet,
                        module_name=module_name,
                        suite=suite,
                        config=prepared,
                        trajectory=trajectory,
                        print_results=print_results,
                        result_collector=collector,
                    )
        finally:
            # A real compiled application's runtime owns model shutdown. The
            # bare-module compatibility path has no such owner; close only it
            # here so custom clients never receive duplicate close calls.
            if not isinstance(application, CompiledApplication):
                await close_owned_eval_model_transports(target, primary_error=sys.exc_info()[1])

    import_root = str(artifact.parent)
    sys.path.insert(0, import_root)
    try:
        try:
            with _isolated_eval_logging(), _adk_eval_output_filter(module_name):
                asyncio.run(evaluate_all())
        except AssertionError as exc:
            print(str(exc), file=sys.stderr)
            _publish_eval_result(
                collector,
                status="failed",
                print_results=print_results,
                result_output=result_output,
            )
            return 1
        except Exception as exc:
            # Preserve any suites completed before an infrastructure failure so
            # CI can distinguish an execution error from a scored eval failure.
            _publish_eval_result(
                collector,
                status="error",
                print_results=print_results,
                result_output=result_output,
                error=exc,
            )
            raise
        _publish_eval_result(
            collector,
            status="passed",
            print_results=print_results,
            result_output=result_output,
        )
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
    result_output: Path | None = None,
) -> int:
    """Run LangGraph through the same ADK metric registry and eval-set runner."""

    AgentEvaluator, EvalSet = _eval_dependencies()
    config = _eval_config(suite, trajectory)
    collector = _EvalResultCollector(framework="langgraph", trajectory=trajectory)
    if config.live_model_config is not None:
        raise AgentTestError(
            "LangGraph --evals does not support ADK liveModelConfig; use text "
            "conversation evals or authored smoke tests for bidirectional media"
        )

    async def evaluate_all() -> None:
        """Borrow graph transports until the owning evaluation driver exits."""

        from .eval_langgraph import langgraph_eval_agent_module

        async with langgraph_eval_agent_module(application) as module_name:
            with eval_model_transports(application.target, config) as prepared:
                await _evaluate_eval_sets(
                    AgentEvaluator,
                    EvalSet,
                    module_name=module_name,
                    suite=suite,
                    config=prepared,
                    trajectory=trajectory,
                    print_results=print_results,
                    result_collector=collector,
                )

    with _isolated_eval_logging():
        try:
            asyncio.run(evaluate_all())
        except AssertionError as exc:
            print(str(exc), file=sys.stderr)
            _publish_eval_result(
                collector,
                status="failed",
                print_results=print_results,
                result_output=result_output,
            )
            return 1
        except Exception as exc:
            # Preserve partial framework-neutral results on adapter or provider
            # errors while allowing the CLI to report the original exception.
            _publish_eval_result(
                collector,
                status="error",
                print_results=print_results,
                result_output=result_output,
                error=exc,
            )
            raise
    _publish_eval_result(
        collector,
        status="passed",
        print_results=print_results,
        result_output=result_output,
    )
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
    result_collector: _EvalResultCollector | None = None,
) -> None:
    """Evaluate every validated suite with consistent output and audit events."""

    scored_failures: list[str] = []
    for path in suite.eval_sets:
        eval_set = eval_set_class.model_validate_json(path.read_text(encoding="utf-8"))
        if result_collector is not None:
            # ADK skips its persistence callback for an empty EvalSet, so
            # register the authored suite before evaluation to keep it visible.
            result_collector.register_eval_set(
                app_name=_EVAL_APP_NAME,
                eval_set_id=eval_set.eval_set_id,
            )
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
                app_name=_EVAL_APP_NAME if result_collector is not None else None,
                eval_set_results_manager=result_collector,
            )
        except AssertionError as exc:
            _EVAL_AUDIT.info(
                "eval.finished",
                trigger="user",
                outcome="failed",
                suite=path.name,
            )
            # Scored failures are expected quality-gate outcomes. Continue so
            # the returned run result covers every independently authored suite.
            scored_failures.append(str(exc))
        except BaseException:
            _EVAL_AUDIT.info(
                "eval.finished",
                trigger="user",
                outcome="failed",
                suite=path.name,
            )
            raise
        else:
            _EVAL_AUDIT.info(
                "eval.finished", trigger="user", outcome="completed", suite=path.name
            )
    if scored_failures:
        raise AssertionError("\n\n".join(scored_failures))


def run_agent_tests(
    source: str | Path,
    *,
    include_smoke: bool = False,
    include_evals: bool = False,
    eval_trajectory: str = "business",
    no_output: bool = False,
    eval_output: Path | None = None,
    framework: str = "adk",
    mode: str = "managed",
    cli_enabled: bool = False,
) -> int:
    """Compile an authored agent and run its convention-based pytest suites."""

    _require_eval_trajectory(eval_trajectory)
    # Reject a dead output target before compiling so direct Python callers get
    # the same contract as the public Go command.
    if eval_output is not None and not include_evals:
        raise AgentTestError("--eval-output requires --evals")
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
                        result_output=eval_output,
                    )
            with _test_output(suppressed=no_output):
                return _run_langgraph_evals(
                    plugin.compiled_application,
                    suite,
                    trajectory=eval_trajectory,
                    print_results=not no_output,
                    result_output=eval_output,
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
