"""Provider-neutral sandbox definitions for ADK code execution."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Lazily construct an ADK code executor from a sandbox backend.

    A sandbox is a code-execution boundary, not a policy label. The backend
    returned by ``factory`` must be an ADK ``BaseCodeExecutor`` and owns the
    actual isolation guarantees. Keeping construction lazy lets compilation
    validate an agent without contacting Docker or a remote sandbox service.
    """

    factory: Callable[[], Any] = field(repr=False)
    backend: str = "custom"
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if not callable(self.factory):
            raise TypeError("sandbox factory must be callable")
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("sandbox backend name is required")
        object.__setattr__(self, "backend", self.backend.strip())
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0
        ):
            raise ValueError("sandbox timeout_seconds must be positive")

    @classmethod
    def provider(
        cls,
        factory: Callable[[], Any],
        *,
        name: str = "custom",
        timeout_seconds: int | None = None,
    ) -> "Sandbox":
        """Use an installed provider package that returns an ADK executor."""

        return cls(
            factory=factory,
            backend=name,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def container(
        cls,
        *,
        image: str | None = None,
        docker_path: str | None = None,
        base_url: str | None = None,
        network: bool = False,
        timeout_seconds: int = 300,
        options: Mapping[str, Any] | None = None,
    ) -> "Sandbox":
        """Use ADK's hardened Docker-backed code executor.

        Either ``image`` or ``docker_path`` is required. Networking is denied
        by default. The optional backend dependency belongs in the agent's
        requirements file: ``google-adk[extensions]``.
        """

        if bool(image) == bool(docker_path):
            raise ValueError("container sandbox requires exactly one of image or docker_path")
        if image is not None and not image.strip():
            raise ValueError("container sandbox image must not be blank")
        if docker_path is not None and not docker_path.strip():
            raise ValueError("container sandbox docker_path must not be blank")
        if base_url is not None and not base_url.strip():
            raise ValueError("container sandbox base_url must not be blank")
        if not isinstance(network, bool):
            raise TypeError("container sandbox network must be a boolean")
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("container sandbox timeout_seconds must be positive")
        extra = dict(options or {})
        reserved = {
            "image",
            "docker_path",
            "base_url",
            "network_enabled",
            "timeout_seconds",
        } & extra.keys()
        if reserved:
            rendered = ", ".join(sorted(reserved))
            raise ValueError(f"container sandbox options repeat reserved fields: {rendered}")

        def build_container() -> Any:
            try:
                from google.adk.code_executors import ContainerCodeExecutor
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "container sandbox requires google-adk[extensions] and Docker"
                ) from exc
            return ContainerCodeExecutor(
                image=image,
                docker_path=docker_path,
                base_url=base_url,
                network_enabled=network,
                timeout_seconds=timeout_seconds,
                **extra,
            )

        return cls(
            factory=build_container,
            backend="container",
            timeout_seconds=timeout_seconds,
        )

    def to_adk_executor(self) -> Any:
        """Return an ADK executor that creates the backend on first execution."""

        try:
            from google.adk.code_executors import BaseCodeExecutor
            from pydantic import PrivateAttr
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("sandbox support requires Google ADK") from exc

        definition = self

        class LazySandboxExecutor(BaseCodeExecutor):
            _delegate: Any = PrivateAttr(default=None)
            _lock: Any = PrivateAttr(default_factory=threading.Lock)

            def execute_code(
                self,
                invocation_context: Any,
                code_execution_input: Any,
            ) -> Any:
                if self._delegate is None:
                    with self._lock:
                        if self._delegate is None:
                            self._delegate = definition.build()
                return self._delegate.execute_code(
                    invocation_context,
                    code_execution_input,
                )

        return LazySandboxExecutor(timeout_seconds=self.timeout_seconds)

    def build(self) -> Any:
        """Construct and validate the configured ADK code executor."""

        try:
            from google.adk.code_executors import BaseCodeExecutor
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("sandbox support requires Google ADK") from exc
        executor = self.factory()
        if not isinstance(executor, BaseCodeExecutor):
            raise TypeError(
                f"sandbox backend {self.backend!r} returned "
                f"{type(executor).__name__}, expected ADK BaseCodeExecutor"
            )
        return executor


__all__ = ["Sandbox"]
