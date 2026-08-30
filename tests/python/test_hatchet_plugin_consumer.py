from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import harnest.plugins as plugin_namespace
from harnest.bundle import compile_artifact
from harnest.checkpoint import RunScope
from harnest.runtime import create_fastapi_app
from harnest.runtime_auth import AuthPrincipal
from harnest.store_postgres_schema import SCHEMA_VERSION


ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
HATCHET_SDK_AVAILABLE = importlib.util.find_spec("hatchet_sdk") is not None
HATCHET_LIVE = os.getenv("HARNEST_HATCHET_LIVE") == "1"
ASYNC_PG_AVAILABLE = importlib.util.find_spec("asyncpg") is not None
REAL_MODEL_LIVE = os.getenv("HARNEST_HATCHET_REAL_LIVE") == "1"
_REAL_LIVE_ENVIRONMENT = (
    "HATCHET_CLIENT_TOKEN",
    "HATCHET_CLIENT_HOST_PORT",
    "HATCHET_CLIENT_TLS_STRATEGY",
    "HATCHET_CLIENT_SERVER_URL",
    "HARNEST_HATCHET_POSTGRES_URL",
    "LITELLM_MODEL",
)
REAL_LIVE_READY = REAL_MODEL_LIVE and all(
    os.getenv(name) for name in _REAL_LIVE_ENVIRONMENT
)
_FIXTURE = Path(__file__).parents[1] / "fixtures" / "hatchet_consumer"
_REAL_FIXTURE = Path(__file__).parents[1] / "fixtures" / "hatchet_consumer_real"
_PRODUCER_PLUGIN = Path(__file__).parents[2] / "examples" / "plugins" / "hatchet"


def _install_hatchet_plugin(source: Path) -> Path:
    """Install the producer-owned plugin exactly as a consumer would attach it."""

    plugin = source / "plugins" / "hatchet"
    shutil.copytree(_PRODUCER_PLUGIN, plugin)
    return plugin


def _real_consumer_source(source: Path) -> None:
    """Overlay only real-model and PostgreSQL choices onto the shared consumer."""

    shutil.copytree(_FIXTURE, source)
    for path in _REAL_FIXTURE.rglob("*"):
        if not path.is_file():
            continue
        target = source / path.relative_to(_REAL_FIXTURE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    _install_hatchet_plugin(source)


class _HeaderAuthenticator:
    """Give the live fixture two explicit principals without external identity."""

    async def authenticate(self, connection) -> AuthPrincipal:
        """Treat the test-only header as already verified identity."""

        return AuthPrincipal(connection.headers.get("x-test-user", "anonymous"))


class _FakeHatchetService:
    """Independently owned deterministic runtime behind the real plugin policy."""

    def __init__(self, module) -> None:
        """Retain only bounded evidence needed by the consumer contract."""

        self.module = module
        self.alive = True
        self.started = Event()
        self.released = Event()
        self.run_id = "fake-hatchet-run-1"
        self.correlation_id: str | None = None
        self.workflow_name: str | None = None
        self.idempotency_key: str | None = None
        self.cancel_count = 0
        self.close_count = 0
        self.scopes: list[tuple[str, ...]] = []

    async def open_transport(self, scopes):
        """Create a short-lived client without transferring service ownership."""

        self.scopes.append(tuple(scopes))
        return _FakeHatchetTransport(self)

    def release(self) -> None:
        """Complete the external job independently from the Harnest request."""

        self.released.set()


class _FakeHatchetTransport:
    """Implement the producer plugin's narrow external transport contract."""

    def __init__(self, service: _FakeHatchetService) -> None:
        self.service = service

    async def run(
        self,
        workflow_name,
        job_input,
        *,
        correlation_id,
        idempotency_key=None,
    ):
        """Record safe identities and start one gated external job."""

        if set(job_input) != {"topic"}:
            raise ValueError("unexpected external job input")
        self.service.workflow_name = workflow_name
        self.service.correlation_id = correlation_id
        self.service.idempotency_key = idempotency_key
        self.service.started.set()
        return self.service.module.HatchetRun(
            self.service.run_id, workflow_name, correlation_id
        )

    async def status(self, job):
        """Expose completion only after the test-controlled release."""

        del job
        if self.service.released.is_set():
            return self.service.module.HatchetRunStatus.COMPLETED
        return self.service.module.HatchetRunStatus.RUNNING

    async def result(self, job):
        """Return deterministic JSON after the external runtime completes."""

        return {
            "report": "external report for quarterly",
            "correlation_id": job.correlation_id,
        }

    async def cancel(self, job) -> None:
        """Record explicit cancellation without stopping the fake service."""

        del job
        self.service.cancel_count += 1

    async def aclose(self) -> None:
        """Close this client while leaving the external service alive."""

        self.service.close_count += 1


def _poll_completed(
    client,
    response_id: str,
    session_id: str,
    headers,
    *,
    attempts: int = 80,
    interval: float = 0.05,
):
    """Poll the principal-scoped status route until the exact run completes."""

    response = None
    for _attempt in range(attempts):
        response = client.get(
            f"/responses/{response_id}",
            params={"sessionId": session_id},
            headers=headers,
        )
        if response.status_code == 404:
            # A freshly started replica may still be rebuilding the opaque
            # pending boundary from shared state; absence is not completion.
            time.sleep(interval)
            continue
        if response.json().get("status") != "in_progress":
            return response
        time.sleep(interval)
    raise AssertionError("external continuation did not complete")


def _execution_events(path: Path) -> list[tuple[str, str]]:
    """Read bounded model/tool evidence emitted by the authored fixture."""

    return [
        (item["component"], item["phase"])
        for item in (
            json.loads(line) for line in path.read_text("utf-8").splitlines()
        )
    ]


def _control_request(path: str, method: str = "GET") -> dict[str, object]:
    """Read one bounded response from the loopback-only worker control plane."""

    request = Request(f"http://127.0.0.1:8099{path}", method=method)
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read(64 * 1024))


def _wait_for_worker(
    correlation_id: str, *, expected_state: str = "started"
) -> dict[str, object]:
    """Wait until the independent Docker worker records the expected state."""

    for _attempt in range(120):
        try:
            evidence = _control_request(f"/evidence/{correlation_id}")
        except HTTPError as error:
            if error.code != 404:
                raise
        else:
            if evidence.get("state") == expected_state:
                return evidence
        time.sleep(0.25)
    raise AssertionError(
        f"Docker Hatchet worker did not reach {expected_state!r}"
    )


async def _postgres_ownership_evidence(dsn: str, scope: RunScope) -> dict[str, int]:
    """Read all durable ownership assertions with one SQL aggregate query."""

    asyncpg = importlib.import_module("asyncpg")
    connection = await asyncpg.connect(dsn)
    try:
        # Every CTE repeats the complete public ownership key so evidence can
        # never be satisfied by a neighboring principal, session, or run.
        row = await connection.fetchrow(
            """
            WITH owned_session AS (
                SELECT state FROM harnest_sessions
                WHERE user_id=$1 AND session_id=$2
            ), owned_run AS (
                SELECT * FROM harnest_runs
                WHERE application_id=$3 AND user_id=$1
                  AND session_id=$2 AND run_id=$4
            ), owned_checkpoints AS (
                SELECT checkpoint.* FROM harnest_checkpoints AS checkpoint
                JOIN owned_run AS run USING (run_id)
            ), owned_continuations AS (
                SELECT continuation.* FROM harnest_continuations AS continuation
                JOIN owned_run AS run USING (run_id)
                WHERE continuation.application_id=$3
                  AND continuation.user_id=$1 AND continuation.session_id=$2
            )
            SELECT
                COALESCE((SELECT version FROM harnest_schema_migrations
                          WHERE component='store'), 0)::int AS schema_version,
                (SELECT count(*) FROM owned_session)::int AS session_count,
                COALESCE((SELECT jsonb_array_length(
                    state->'_harnest_adk_events') FROM owned_session), 0)::int
                    AS session_event_count,
                (SELECT count(*) FROM owned_run)::int AS run_count,
                (SELECT count(*) FROM owned_run
                 WHERE status='completed' AND framework='adk')::int
                    AS completed_adk_run_count,
                COALESCE((SELECT max(revision) FROM owned_run), -1)::int
                    AS run_revision,
                (SELECT count(*) FROM owned_run
                 WHERE pending_action IS NULL)::int AS cleared_run_count,
                (SELECT count(*) FROM owned_checkpoints)::int
                    AS checkpoint_count,
                (SELECT count(*) FROM owned_checkpoints
                 WHERE framework='adk')::int AS adk_checkpoint_count,
                (SELECT count(*) FROM owned_checkpoints
                 WHERE namespace='events')::int AS event_checkpoint_count,
                (SELECT count(*) FROM owned_checkpoints
                 WHERE parent_checkpoint_id IS NULL)::int
                    AS root_checkpoint_count,
                (SELECT count(*) FROM owned_checkpoints AS child
                 LEFT JOIN owned_checkpoints AS parent
                   ON parent.checkpoint_id=child.parent_checkpoint_id
                  AND parent.namespace=child.namespace
                 WHERE child.parent_checkpoint_id IS NOT NULL
                   AND parent.checkpoint_id IS NULL)::int
                    AS broken_checkpoint_link_count,
                (SELECT count(*) FROM owned_continuations)::int
                    AS continuation_count,
                (SELECT count(*) FROM owned_continuations
                 WHERE status='claimed')::int AS claimed_continuation_count,
                COALESCE((SELECT max(revision)
                          FROM owned_continuations), -1)::int
                    AS continuation_revision,
                (SELECT count(*) FROM owned_continuations
                 WHERE provider='hatchet' AND capability='hatchet.run'
                   AND schema_id='hatchet.run-result.v1'
                   AND result IS NOT NULL AND failure IS NULL)::int
                    AS valid_continuation_field_count
            """,
            scope.user_id,
            scope.session_id,
            scope.application_id,
            scope.run_id,
        )
        return dict(row)
    finally:
        await connection.close()


async def _delete_postgres_live_records(dsn: str, scope: RunScope) -> None:
    """Delete only records created under this test's unique ownership scope."""

    asyncpg = importlib.import_module("asyncpg")
    connection = await asyncpg.connect(dsn)
    try:
        # Run deletion cascades private checkpoints and continuations; the
        # session remains independent by design and is removed in the same SQL.
        await connection.execute(
            """
            WITH deleted_run AS (
                DELETE FROM harnest_runs
                WHERE application_id=$3 AND user_id=$1
                  AND session_id=$2 AND run_id=$4
                RETURNING run_id
            )
            DELETE FROM harnest_sessions
            WHERE user_id=$1 AND session_id=$2
              AND EXISTS (SELECT 1 FROM deleted_run)
            """,
            scope.user_id,
            scope.session_id,
            scope.application_id,
            scope.run_id,
        )
    finally:
        await connection.close()


@unittest.skipUnless(ADK_AVAILABLE, "Google ADK is required")
class HatchetConsumerCompilerTests(unittest.TestCase):
    def test_fresh_agent_compiles_against_only_the_plugin_public_api(self):
        """Prove filesystem installation and namespace discovery stay decoupled."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "source", root / "artifact"
            shutil.copytree(_FIXTURE, source)
            plugin = _install_hatchet_plugin(source)

            # The external-runtime adapter is a native capability, not an
            # agent-plugin: the consumer must remain the sole tool author.
            self.assertFalse((plugin / "tools").exists())
            compile_artifact(source, artifact, framework="adk")

            manifest = json.loads(
                (artifact / "harnest-manifest.json").read_text("utf-8")
            )
            self.assertEqual(
                [item["name"] for item in manifest["plugins"]], ["hatchet"]
            )
            files = {item["path"] for item in manifest["files"]}
            self.assertIn("source/tools/create_report_job.py", files)
            self.assertIn("source/plugins/hatchet/plugin.py", files)
            self.assertFalse(
                any(path.startswith("source/plugins/hatchet/tools/") for path in files)
            )

        # Artifact compilation must release the temporary public namespace so
        # the next independently compiled agent cannot inherit this plugin.
        self.assertFalse(hasattr(plugin_namespace, "hatchet"))
        self.assertNotIn("harnest.plugins.hatchet", sys.modules)

    def test_consumer_does_not_import_the_external_sdk_or_plugin_internals(self):
        """Keep the reusable adapter boundary observable in the static fixture."""

        tool_source = (_FIXTURE / "tools" / "create_report_job.py").read_text(
            "utf-8"
        )
        self.assertIn("from harnest.plugins.hatchet import hatchet", tool_source)
        self.assertNotIn("from hatchet", tool_source)
        self.assertNotIn("plugins.hatchet.plugin", tool_source)
        self.assertIn("await hatchet.run(", tool_source)
        self.assertIn("await hatchet.wait(", tool_source)
        project = (_FIXTURE / "pyproject.toml").read_text("utf-8")
        self.assertIn('"hatchet-sdk>=1.38,<2"', project)
        self.assertNotIn('"harnest', project)

    def test_real_overlay_compiles_without_model_or_database_io(self):
        """Keep the gated provider/PostgreSQL variant statically verifiable."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "source", root / "artifact"
            with patch.dict(
                "os.environ",
                {
                    "LITELLM_MODEL": "openai/offline-compile-only",
                    "HARNEST_HATCHET_POSTGRES_URL": (
                        "postgresql://offline:offline@127.0.0.1:1/offline"
                    ),
                },
            ):
                _real_consumer_source(source)
                compile_artifact(source, artifact, framework="adk")

            manifest = json.loads(
                (artifact / "harnest-manifest.json").read_text("utf-8")
            )
            files = {item["path"] for item in manifest["files"]}
            self.assertIn("source/extensions/model_evidence.py", files)
            self.assertIn("source/extensions/storage.py", files)
            self.assertIn("source/plugins/hatchet/plugin.py", files)
            project = (source / "pyproject.toml").read_text("utf-8")
            self.assertIn('"asyncpg>=0.30,<1"', project)
            self.assertIn('"litellm>=1.84,<2"', project)

        self.assertFalse(hasattr(plugin_namespace, "hatchet"))
        self.assertNotIn("harnest.plugins.hatchet", sys.modules)


@unittest.skipUnless(ADK_AVAILABLE, "Google ADK is required")
class HatchetConsumerRuntimeTests(unittest.TestCase):
    def test_external_completion_resumes_adk_model_loop_and_session(self):
        """Cross compiler, plugin, continuation, HTTP, framework, and shutdown."""

        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "source", root / "artifact"
            evidence = root / "consumer-events.jsonl"
            shutil.copytree(_FIXTURE, source)
            _install_hatchet_plugin(source)
            compile_artifact(source, artifact, framework="adk")

            with patch.dict(
                "os.environ",
                {"HARNEST_HATCHET_CONSUMER_EVENTS": str(evidence)},
            ):
                app = create_fastapi_app(
                    artifact,
                    authenticator=_HeaderAuthenticator(),
                    playground_enabled=False,
                )
                module = plugin_namespace.hatchet
                plugin = module.plugin
                service = _FakeHatchetService(module)
                with patch.object(
                    module,
                    "open_hatchet_transport",
                    new=service.open_transport,
                ):
                    self._exercise_runtime(TestClient, app, service, evidence)

                self.assertFalse(plugin._started)
                self.assertTrue(service.alive)
                self.assertEqual(service.cancel_count, 0)
                self.assertGreaterEqual(service.close_count, 2)

            self.assertFalse(hasattr(plugin_namespace, "hatchet"))
            self.assertNotIn("harnest.plugins.hatchet", sys.modules)

    def _exercise_runtime(self, test_client, app, service, evidence: Path) -> None:
        """Drive one principal-owned wait through completion and cleanup."""

        alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
        with test_client(app) as client:
            created = client.post(
                "/sessions", headers=alice, json={"id": "hatchet-session"}
            )
            other = client.post(
                "/sessions", headers=alice, json={"id": "other-session"}
            )
            self.assertEqual((created.status_code, other.status_code), (201, 201))
            initial = client.post(
                "/responses",
                headers=alice,
                json={"input": "create report", "sessionId": "hatchet-session"},
            )
            self.assertEqual(initial.status_code, 200)
            pending = initial.json()
            self._assert_pending(pending, service)
            self._assert_scope_isolation(client, pending["id"], alice, bob)
            self.assertEqual(
                _execution_events(evidence),
                [
                    ("model", "submit"),
                    ("tool", "enter"),
                    ("tool", "submitted"),
                ],
            )

            service.release()
            completed = _poll_completed(
                client, pending["id"], "hatchet-session", alice
            )
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(completed.json()["status"], "completed")
            self.assertIn("external report for quarterly", completed.json()["outputText"])
            self._assert_final_session(client, alice)

        self.assertEqual(
            _execution_events(evidence),
            [
                ("model", "submit"),
                ("tool", "enter"),
                ("tool", "submitted"),
                ("model", "finish"),
            ],
        )

    def _assert_pending(self, pending, service: _FakeHatchetService) -> None:
        """Verify public suspension and private provider correlation identities."""

        self.assertEqual(pending["status"], "in_progress")
        self.assertEqual(pending["pendingAction"]["type"], "external_continuation")
        self.assertEqual(pending["pendingAction"]["capability"], "hatchet.run")
        self.assertNotEqual(pending["pendingAction"]["id"], service.run_id)
        self.assertNotIn(service.run_id, json.dumps(pending))
        self.assertTrue(service.started.wait(timeout=1))
        self.assertEqual(service.correlation_id, pending["id"])
        self.assertEqual(service.workflow_name, "consumer-report")

    def _assert_scope_isolation(self, client, response_id, alice, bob) -> None:
        """Hide response existence across either principal or session boundary."""

        path = f"/responses/{response_id}"
        same = client.get(
            path, params={"sessionId": "hatchet-session"}, headers=alice
        )
        foreign_user = client.get(
            path, params={"sessionId": "hatchet-session"}, headers=bob
        )
        foreign_session = client.get(
            path, params={"sessionId": "other-session"}, headers=alice
        )
        self.assertEqual(same.json()["status"], "in_progress")
        self.assertEqual(
            (foreign_user.status_code, foreign_session.status_code), (404, 404)
        )

    def _assert_final_session(self, client, headers) -> None:
        """Require the resumed assistant output in the original transcript."""

        transcript = client.get(
            "/sessions/hatchet-session/messages", headers=headers
        )
        self.assertEqual(transcript.status_code, 200)
        assistants = [
            item
            for item in transcript.json()["messages"]
            if item["role"] == "assistant"
        ]
        self.assertTrue(assistants)
        self.assertIn("external report for quarterly", json.dumps(assistants[-1]))


@unittest.skipUnless(
    ADK_AVAILABLE and HATCHET_SDK_AVAILABLE and HATCHET_LIVE,
    "set HARNEST_HATCHET_LIVE=1 and install hatchet-sdk for Docker live test",
)
class HatchetConsumerDockerLiveTests(unittest.TestCase):
    def test_real_external_worker_resumes_compiled_consumer_without_replay(self):
        """Prove the black-box consumer journey against separately owned Docker."""

        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "source", root / "artifact"
            evidence = root / "consumer-events.jsonl"
            shutil.copytree(_FIXTURE, source)
            _install_hatchet_plugin(source)
            compile_artifact(source, artifact, framework="adk")
            with patch.dict(
                "os.environ",
                {"HARNEST_HATCHET_CONSUMER_EVENTS": str(evidence)},
            ):
                app = create_fastapi_app(
                    artifact,
                    authenticator=_HeaderAuthenticator(),
                    playground_enabled=False,
                )
                self._exercise_docker(TestClient, app, evidence)

            self.assertFalse(hasattr(plugin_namespace, "hatchet"))
            self.assertEqual(_control_request("/healthz"), {"status": "ok"})

    def _exercise_docker(self, test_client, app, evidence: Path) -> None:
        """Suspend through the real SDK, release the worker, and poll the result."""

        alice = {"x-test-user": "alice"}
        correlation_id = None
        try:
            with test_client(app) as client:
                created = client.post(
                    "/sessions", headers=alice, json={"id": "hatchet-live"}
                )
                self.assertEqual(created.status_code, 201)
                response = client.post(
                    "/responses",
                    headers=alice,
                    json={"input": "create report", "sessionId": "hatchet-live"},
                )
                self.assertEqual(response.status_code, 200)
                pending = response.json()
                self.assertEqual(pending["status"], "in_progress")
                correlation_id = pending["id"]
                worker = _wait_for_worker(correlation_id)
                self.assertNotEqual(
                    pending["pendingAction"]["id"], worker["workflow_run_id"]
                )
                self.assertNotIn(worker["workflow_run_id"], json.dumps(pending))
                _control_request(f"/release/{correlation_id}", "POST")
                completed = _poll_completed(
                    client, correlation_id, "hatchet-live", alice
                )
                self.assertEqual(completed.json()["status"], "completed")
                self.assertIn(
                    "external report for quarterly", completed.json()["outputText"]
                )
        finally:
            if correlation_id is not None:
                try:
                    _control_request(f"/release/{correlation_id}", "POST")
                except OSError:
                    pass

        self.assertEqual(
            _execution_events(evidence),
            [
                ("model", "submit"),
                ("tool", "enter"),
                ("tool", "submitted"),
                ("model", "finish"),
            ],
        )


@unittest.skipUnless(
    ADK_AVAILABLE
    and HATCHET_SDK_AVAILABLE
    and ASYNC_PG_AVAILABLE
    and REAL_LIVE_READY,
    "set HARNEST_HATCHET_REAL_LIVE=1 and the documented live environment",
)
class HatchetConsumerRealModelPostgresLiveTests(unittest.TestCase):
    def test_real_model_postgres_and_hatchet_resume_on_new_replica(self):
        """Let replica B recover replica A's PostgreSQL-owned Hatchet wait."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = root / "source", root / "artifact"
            evidence = root / "consumer-events.jsonl"
            _real_consumer_source(source)
            compile_artifact(source, artifact, framework="adk")
            session_id = f"hatchet-real-{uuid4().hex}"
            dsn = os.environ["HARNEST_HATCHET_POSTGRES_URL"]
            correlation_id = None
            scope = None
            try:
                with patch.dict(
                    "os.environ",
                    {"HARNEST_HATCHET_CONSUMER_EVENTS": str(evidence)},
                ):
                    pending, completed, transcript, provider_run_id = (
                        self._execute_real_journey(artifact, session_id)
                    )
                    correlation_id = pending["id"]
                    scope = RunScope(
                        "hatchet_consumer", "alice", session_id, correlation_id
                    )
                    durable = asyncio.run(
                        _postgres_ownership_evidence(dsn, scope)
                    )
                    self._assert_durable_ownership(durable)
                    restored = self._read_after_restart(artifact, session_id)

                self._assert_private_provider_id(
                    provider_run_id,
                    pending,
                    completed,
                    transcript,
                    durable,
                    restored,
                )
                self._export_public_transcript(transcript)
                self._assert_execution_evidence(evidence)
                self.assertFalse(hasattr(plugin_namespace, "hatchet"))
                self.assertEqual(_control_request("/healthz"), {"status": "ok"})
                _wait_for_worker(correlation_id, expected_state="completed")
            finally:
                if correlation_id is not None:
                    try:
                        _control_request(f"/release/{correlation_id}", "POST")
                    except OSError:
                        pass
                if scope is not None:
                    asyncio.run(_delete_postgres_live_records(dsn, scope))

    def _execute_real_journey(
        self, artifact: Path, session_id: str
    ) -> tuple[dict, dict, dict, str]:
        """Stop replica A while waiting, then release work after B has started."""

        from fastapi.testclient import TestClient

        alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
        first_app = create_fastapi_app(
            artifact,
            authenticator=_HeaderAuthenticator(),
            playground_enabled=False,
        )
        with TestClient(first_app) as client:
            created = client.post(
                "/sessions", headers=alice, json={"id": session_id}
            )
            self.assertEqual(created.status_code, 201)
            response = client.post(
                "/responses",
                headers=alice,
                json={"input": "create report", "sessionId": session_id},
            )
            self.assertEqual(response.status_code, 200)
            pending = response.json()
            self.assertEqual(pending["status"], "in_progress")
            worker = _wait_for_worker(pending["id"])
            provider_run_id = worker["workflow_run_id"]
            self.assertIsInstance(provider_run_id, str)
            self.assertTrue(provider_run_id)
            self._assert_live_scope_isolation(
                client, pending["id"], session_id, alice, bob
            )

        # The first lifespan has fully stopped before B discovers the pending
        # provider page, proving no process-local poller owns the completion.
        second_app = create_fastapi_app(
            artifact,
            authenticator=_HeaderAuthenticator(),
            playground_enabled=False,
        )
        with TestClient(second_app) as client:
            _control_request(f"/release/{pending['id']}", "POST")
            completed_response = _poll_completed(
                client,
                pending["id"],
                session_id,
                alice,
                attempts=1200,
                interval=0.1,
            )
            self.assertEqual(completed_response.status_code, 200)
            completed = completed_response.json()
            self.assertEqual(completed["status"], "completed")
            self.assertIn("external report for quarterly", completed["outputText"])
            transcript_response = client.get(
                f"/sessions/{session_id}/messages", headers=alice
            )
            self.assertEqual(transcript_response.status_code, 200)
            transcript = transcript_response.json()
            self.assertIn("external report for quarterly", json.dumps(transcript))
        return pending, completed, transcript, provider_run_id

    def _read_after_restart(self, artifact: Path, session_id: str) -> dict:
        """Prove a new server instance reads the prior PostgreSQL transcript."""

        from fastapi.testclient import TestClient

        headers = {"x-test-user": "alice"}
        app = create_fastapi_app(
            artifact,
            authenticator=_HeaderAuthenticator(),
            playground_enabled=False,
        )
        with TestClient(app) as client:
            session = client.get(f"/sessions/{session_id}", headers=headers)
            transcript = client.get(
                f"/sessions/{session_id}/messages", headers=headers
            )
            self.assertEqual((session.status_code, transcript.status_code), (200, 200))
            restored = {"session": session.json(), "transcript": transcript.json()}
            self.assertIn("external report for quarterly", json.dumps(restored))
            return restored

    def _assert_live_scope_isolation(
        self, client, response_id: str, session_id: str, alice, bob
    ) -> None:
        """Keep the durable response hidden across principal and session keys."""

        path = f"/responses/{response_id}"
        same = client.get(path, params={"sessionId": session_id}, headers=alice)
        other_user = client.get(path, params={"sessionId": session_id}, headers=bob)
        other_session = client.get(
            path, params={"sessionId": f"other-{session_id}"}, headers=alice
        )
        self.assertEqual(same.json()["status"], "in_progress")
        self.assertEqual((other_user.status_code, other_session.status_code), (404, 404))

    def _assert_durable_ownership(self, evidence: dict[str, int]) -> None:
        """Require one completed ADK run, one claim, and persisted checkpoints."""

        self.assertEqual(evidence["schema_version"], SCHEMA_VERSION)
        self.assertEqual(evidence["session_count"], 1)
        self.assertGreater(evidence["session_event_count"], 0)
        self.assertEqual(evidence["run_count"], 1)
        self.assertEqual(evidence["completed_adk_run_count"], 1)
        self.assertGreaterEqual(evidence["run_revision"], 3)
        self.assertEqual(evidence["cleared_run_count"], 1)
        self.assertGreater(evidence["checkpoint_count"], 0)
        self.assertEqual(
            evidence["adk_checkpoint_count"], evidence["checkpoint_count"]
        )
        self.assertEqual(
            evidence["event_checkpoint_count"], evidence["checkpoint_count"]
        )
        self.assertEqual(evidence["root_checkpoint_count"], 1)
        self.assertEqual(evidence["broken_checkpoint_link_count"], 0)
        self.assertEqual(evidence["continuation_count"], 1)
        self.assertEqual(evidence["claimed_continuation_count"], 1)
        self.assertGreaterEqual(evidence["continuation_revision"], 2)
        self.assertEqual(evidence["valid_continuation_field_count"], 1)

    def _assert_private_provider_id(self, provider_run_id: str, *documents) -> None:
        """Keep the Hatchet workflow identity outside every public artifact."""

        public = json.dumps(documents, sort_keys=True)
        self.assertNotIn(provider_run_id, public)
        pending = documents[0]
        self.assertNotEqual(pending["pendingAction"]["id"], provider_run_id)

    def _assert_execution_evidence(self, evidence: Path) -> None:
        """Require two real provider calls and exactly one tool execution."""

        events = _execution_events(evidence)
        self.assertEqual(
            events,
            [
                ("model_provider", "before"),
                ("model_provider", "after"),
                ("tool", "enter"),
                ("tool", "submitted"),
                ("model_provider", "before"),
                ("model_provider", "after"),
            ],
        )
        self.assertEqual(events.count(("tool", "enter")), 1)
        self.assertEqual(events.count(("model_provider", "before")), 2)
        self.assertEqual(events.count(("model_provider", "after")), 2)

    def _export_public_transcript(self, transcript: dict) -> None:
        """Optionally retain the privacy-checked HTTP transcript for inspection."""

        target = os.environ.get("HARNEST_HATCHET_TRANSCRIPT_OUTPUT")
        if target:
            Path(target).write_text(
                json.dumps(transcript, indent=2, sort_keys=True),
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
