from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


_LIVE = os.getenv("HARNEST_HATCHET_LIVE") == "1"
_SDK_AVAILABLE = importlib.util.find_spec("hatchet_sdk") is not None
_CLIENT_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "plugins"
    / "hatchet"
    / "lib"
    / "client.py"
)


@unittest.skipUnless(
    _LIVE and _SDK_AVAILABLE,
    "set HARNEST_HATCHET_LIVE=1 and install hatchet-sdk for Docker live test",
)
class HatchetPluginDockerLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_run_is_correlated_released_and_completed(self):
        """Exercise the official SDK against the separately deployed Docker worker."""

        module = _load_client_module()
        token = os.environ.get("HATCHET_CLIENT_TOKEN", "")
        self.assertTrue(token, "HATCHET_CLIENT_TOKEN is required for live test")
        transport = module.HatchetSDKTransport(token)
        correlation_id = f"harnest-live-{uuid4()}"
        job = None
        try:
            await _wait_for_control_health()
            job = await transport.run(
                "consumer-report",
                {"topic": "docker live test"},
                correlation_id=correlation_id,
            )
            evidence = await _wait_for_evidence(correlation_id, "started")
            self.assertEqual(evidence["workflow_run_id"], job.run_id)
            await _post_control(f"/release/{correlation_id}")
            await _wait_for_status(
                transport, job, module.HatchetRunStatus.COMPLETED
            )
            result = await transport.result(job)
            self.assertEqual(result["correlation_id"], correlation_id)
        finally:
            if job is not None:
                await _post_control(f"/release/{correlation_id}", tolerate_error=True)
            await transport.aclose()


def _load_client_module():
    """Load the real reusable adapter without requiring an active plugin namespace."""

    name = "_harnest_hatchet_live_client"
    spec = importlib.util.spec_from_file_location(name, _CLIENT_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Hatchet extension client adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def _wait_for_control_health() -> None:
    """Wait for the external worker control plane without inspecting containers."""

    for _attempt in range(60):
        try:
            await _get_control("/healthz")
            return
        except (OSError, HTTPError):
            await asyncio.sleep(0.5)
    raise AssertionError("Hatchet fixture worker did not become healthy")


async def _wait_for_evidence(
    correlation_id: str, expected_state: str
) -> dict[str, object]:
    """Wait until the independent worker records the expected run state."""

    for _attempt in range(120):
        try:
            evidence = await _get_control(f"/evidence/{correlation_id}")
        except HTTPError as error:
            if error.code != 404:
                raise
        else:
            if evidence.get("state") == expected_state:
                return evidence
        await asyncio.sleep(0.25)
    raise AssertionError(f"worker did not reach {expected_state}")


async def _wait_for_status(transport, job, expected) -> None:
    """Poll the public plugin transport until Hatchet commits completion."""

    for _attempt in range(120):
        if await transport.status(job) is expected:
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"Hatchet run did not reach {expected.value}")


async def _get_control(path: str) -> dict[str, object]:
    """Read one local fixture response off the event-loop thread."""

    return await asyncio.to_thread(_request_control, path, "GET")


async def _post_control(
    path: str, *, tolerate_error: bool = False
) -> dict[str, object]:
    """Write one local fixture operation while optionally tolerating cleanup races."""

    try:
        return await asyncio.to_thread(_request_control, path, "POST")
    except OSError:
        if not tolerate_error:
            raise
        return {}


def _request_control(path: str, method: str) -> dict[str, object]:
    """Decode one bounded JSON document from the loopback-only control server."""

    request = Request(f"http://127.0.0.1:8099{path}", method=method)
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read(64 * 1024))
