"""Pytest integration for import-free authored agent tests."""

from __future__ import annotations

import asyncio
import json
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
from .runtime import create_fastapi_app, load_compiled_agent


class AgentTestError(RuntimeError):
    """An authored test suite does not follow the Harnest convention."""


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
        self.compiled_agent = load_compiled_agent(artifact)
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

    @pytest.fixture
    def client(self, request: Any) -> Iterator[Any]:
        self._require_smoke(request, "client")
        from fastapi.testclient import TestClient

        app = create_fastapi_app(self.artifact, bind_host="testserver")
        with TestClient(app) as test_client:
            yield test_client

    @pytest.fixture
    def smoke(
        self,
        request: Any,
        client: Any,
    ) -> SmokeClient:
        self._require_smoke(request, "smoke")
        return SmokeClient(client)


def _run_adk_evals(artifact: Path, suite: EvalSuite) -> int:
    """Run validated eval sets through ADK's official evaluator."""

    try:
        from google.adk.evaluation.agent_evaluator import AgentEvaluator
        from google.adk.evaluation.eval_config import (
            get_evaluation_criteria_or_default,
        )
        from google.adk.evaluation.eval_set import EvalSet
    except ImportError as exc:  # pragma: no cover - declared ADK eval extra
        raise AgentTestError(
            "Google ADK evaluation dependencies are required for --evals"
        ) from exc

    config = get_evaluation_criteria_or_default(
        str(suite.config) if suite.config is not None else None
    )
    module_name = f"{artifact.name}.agent"

    async def evaluate_all() -> None:
        for path in suite.eval_sets:
            eval_set = EvalSet.model_validate_json(path.read_text(encoding="utf-8"))
            print(f"harnest eval: {path.name}")
            await AgentEvaluator.evaluate_eval_set(
                agent_module=module_name,
                eval_set=eval_set,
                eval_config=config,
                num_runs=1,
                print_detailed_results=True,
            )

    import_root = str(artifact.parent)
    sys.path.insert(0, import_root)
    try:
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


def run_agent_tests(
    source: str | Path,
    *,
    include_smoke: bool = False,
    include_evals: bool = False,
    framework: str = "adk",
    mode: str = "managed",
) -> int:
    """Compile an authored agent and run its convention-based pytest suites."""

    source_directory = Path(source)
    if source_directory.is_file():
        source_directory = source_directory.parent
    source_directory = source_directory.resolve()

    with tempfile.TemporaryDirectory(prefix="harnest-test-") as temp_directory:
        artifact = Path(temp_directory) / "compiled_agent"
        compile_artifact(
            source_directory, artifact, framework=framework, mode=mode
        )
        selected = _selected_test_directories(artifact, include_smoke)
        plugin = _HarnestPytestPlugin(artifact, include_smoke=include_smoke)
        args = _pytest_args(selected)
        test_status = int(pytest.main(args, plugins=[plugin]))
        if test_status != 0 or not include_evals:
            return test_status

        if framework != "adk":
            raise AgentTestError(
                "--evals currently supports ADK EvalSet files only; use authored "
                "pytest evaluations for LangGraph"
            )
        suite = discover_evals(artifact / "source" / "agent.py")
        if not suite.eval_sets:
            raise AgentTestError(
                "--evals requires evals/ with at least one *.evalset.json"
            )
        return _run_adk_evals(artifact, suite)


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


def _pytest_args(selected: list[Path]) -> list[str]:
    return [
        *(str(path) for path in selected),
        "-q", "--disable-warnings", "-W",
        "ignore::pytest.PytestAssertRewriteWarning",
        "-p", "no:cacheprovider", "-p", "no:anyio",
    ]


__all__ = ["AgentTestError", "SmokeClient", "run_agent_tests"]
