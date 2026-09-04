"""Capture native Docker resource ownership before its startup can fail."""

from __future__ import annotations

from typing import Any

from .sandbox_control import current_control


def check_startup() -> None:
    """Reject late native startup work without resetting the invocation deadline."""
    control = current_control()
    if control is not None:
        control.check()


class OwnedStartupClient:
    """Keep the native client policy while intercepting its resource acquisition."""

    def __init__(self, owner: Any, client: Any, *, limits: dict[str, Any] | None = None) -> None:
        """Use a dedicated native client's existing finite control-plane timeout."""
        self._client = client
        self.containers = OwnedStartupContainers(owner, client.containers, limits=limits)

    def __getattr__(self, name: str) -> Any:
        """Forward images, configuration, and cleanup unchanged to native Docker."""
        return getattr(self._client, name)


class OwnedStartupContainers:
    """Run SDK startup verbatim but retain each created resource before start."""

    def __init__(self, owner: Any, containers: Any, *, limits: dict[str, Any] | None = None) -> None:
        """Capture the only container collection this executor is allowed to own."""
        self.owner, self._containers = owner, containers
        self._limits = limits or {}

    def __getattr__(self, name: str) -> Any:
        """Preserve the collection's client and native image-pull fallback."""
        return getattr(self._containers, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Keep Docker's native run behavior and check admission around its I/O."""
        from docker.models.containers import ContainerCollection

        check_startup()
        container = ContainerCollection.run(self, *args, **kwargs)
        check_startup()
        return container

    def create(self, *args: Any, **kwargs: Any) -> Any:
        """Record ownership before native run calls start, which itself can fail."""
        from docker.errors import ImageNotFound

        check_startup()
        try:
            # Apply policy last: native defaults cannot weaken authored hard limits.
            container = self._containers.create(*args, **(kwargs | self._limits))
        except ImageNotFound:
            # This explicit rejection is safe for native pull-and-retry.
            raise
        except Exception:
            # A lost create response can hide an allocated ID. Without an ID
            # we cannot prove cleanup, so automatic replacement must stop.
            self.owner._startup_uncertain = True
            raise
        self.owner._container = container
        check_startup()
        return container
