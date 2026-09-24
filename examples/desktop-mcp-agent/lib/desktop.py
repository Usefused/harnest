"""Host-side owner and narrow HTTP client for the persistent desktop."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

import httpx


@dataclass(slots=True)
class Desktop:
    """Hold the single container until the Harnest lifecycle closes it."""

    container: object
    docker_client: object
    base_url: str
    token: str

    @classmethod
    def start(cls) -> "Desktop":
        """Start a named desktop and prove Chrome readiness before serving."""
        import docker

        instance = os.environ["HARNEST_DESKTOP_INSTANCE"]
        port = int(os.environ["HARNEST_DESKTOP_API_PORT"])
        token = os.environ["HARNEST_DESKTOP_TOKEN"]
        image = os.environ.get("HARNEST_DESKTOP_IMAGE", "harnest-linux-desktop:2")
        client = docker.from_env(timeout=10)
        container = None
        try:
            ports = {"8765/tcp": ("127.0.0.1", port)}
            viewer = os.environ.get("HARNEST_DESKTOP_VIEWER_PORT")
            if viewer:
                ports["6080/tcp"] = ("127.0.0.1", int(viewer))
            container = client.containers.run(
                image,
                platform="linux/amd64",
                name=f"harnest-desktop-{instance}",
                detach=True,
                init=True,
                ports=ports,
                environment={
                    "DESKTOP_TOKEN": token,
                    "ENABLE_VIEWER": "1" if viewer else "0",
                    "ALLOWED_HOSTS": os.environ.get("DESKTOP_ALLOWED_HOSTS", ""),
                },
                labels={"dev.harnest.managed": "true", "dev.harnest.desktop.instance": instance},
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="2g",
                nano_cpus=1_000_000_000,
                pids_limit=256,
                tmpfs={"/tmp": "rw,nosuid,nodev,size=512m,mode=1777"},
            )
            base_url = f"http://127.0.0.1:{port}"
            _wait_ready(base_url, token, container)
            return cls(container, client, base_url, token)
        except BaseException:
            if container is not None:
                _remove_container(container)
            client.close()
            raise

    async def request(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """Call only the loopback-published desktop API with its private token."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=25) as client:
            response = await client.request(
                method, path, json=json, headers={"Authorization": f"Bearer {self.token}"}
            )
            response.raise_for_status()
            return response

    def close(self) -> None:
        """Remove the owned container when the agent runtime stops."""
        try:
            _remove_container(self.container)
        finally:
            self.docker_client.close()


def _wait_ready(base_url: str, token: str, container: object) -> None:
    """Bound startup and fail if the desktop exits before Chrome is ready."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        container.reload()
        if container.status != "running":
            raise RuntimeError("desktop container stopped during startup")
        request = Request(base_url + "/healthz", headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.25)
    raise TimeoutError("desktop did not become ready within 60 seconds")


def _remove_container(container: object) -> None:
    """Stop the exact owned container before removing it from Docker."""
    try:
        container.stop(timeout=5)
    finally:
        container.remove(force=True)
