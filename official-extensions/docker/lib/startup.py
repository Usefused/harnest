"""Capture Docker resource ownership before startup can fail."""

from __future__ import annotations

from typing import Any

from harnest.sandbox import control


def check_startup() -> None:
    """Reject late Docker startup work without renewing the invocation deadline."""

    current = control.current()
    if current is not None:
        current.check()


def constrain_startup_timeout(client: Any, maximum: float) -> float:
    """Bound the next Docker API call by the remaining startup authority."""

    current = control.current()
    remaining = None if current is None else current.remaining()
    timeout = maximum if remaining is None else min(maximum, remaining)
    # urllib3 rejects zero while the surrounding control check owns the exact
    # deadline edge; a millisecond still keeps the native call tightly bounded.
    timeout = max(0.001, timeout)
    client.api.timeout = timeout
    return timeout


class OwnedStartupClient:
    """Keep client behavior while intercepting container resource acquisition."""

    def __init__(
        self,
        owner: Any,
        client: Any,
        *,
        limits: dict[str, Any] | None = None,
    ) -> None:
        """Wrap a dedicated client's container collection with hard limits."""

        self._client = client
        self.containers = OwnedStartupContainers(
            owner, client.containers, limits=limits
        )

    def __getattr__(self, name: str) -> Any:
        """Forward image, configuration, and cleanup calls to the Docker client."""

        return getattr(self._client, name)


class OwnedStartupContainers:
    """Retain each created container before a later start operation can fail."""

    def __init__(
        self,
        owner: Any,
        containers: Any,
        *,
        limits: dict[str, Any] | None = None,
    ) -> None:
        """Capture the only container collection this executor may own."""

        self.owner = owner
        self._containers = containers
        self._limits = limits or {}

    def __getattr__(self, name: str) -> Any:
        """Preserve the collection's native client and image-pull fallback."""

        return getattr(self._containers, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Keep Docker's run behavior while checking authority around its I/O."""

        from docker.models.containers import ContainerCollection

        check_startup()
        container = ContainerCollection.run(self, *args, **kwargs)
        check_startup()
        return container

    def create(self, *args: Any, **kwargs: Any) -> Any:
        """Record ownership before Docker starts the allocated container."""

        from docker.errors import ImageNotFound

        check_startup()
        constrain_startup_timeout(
            self._containers.client, self.owner.timeout_seconds
        )
        try:
            # Provider policy wins over all Docker SDK defaults and caller input.
            container = self._containers.create(*args, **(kwargs | self._limits))
        except ImageNotFound:
            raise
        except Exception:
            # A lost response can hide an allocated ID, so replacement is unsafe.
            self.owner._startup_uncertain = True
            raise
        self.owner._container = container
        check_startup()
        return container


__all__ = [
    "OwnedStartupClient",
    "check_startup",
    "constrain_startup_timeout",
]
