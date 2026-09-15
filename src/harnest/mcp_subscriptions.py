"""Explicit, application-owned MCP resource listeners with latest-value recovery."""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
from typing import Any, Awaitable, Callable, Literal, Mapping

from .mcp_lifecycle import MCPClientLifecycle
from .mcp_resources import MCPResourceError, _bounded_result, resource_session


@dataclass(frozen=True, slots=True)
class MCPResourceEvent:
    """One retained resource value; handlers decide whether to invoke an agent or task."""

    client_name: str
    uri: str
    resource: Mapping[str, Any] = field(repr=False)
    reason: Literal["start", "update", "reconnect"] = "update"


@dataclass(frozen=True, slots=True)
class MCPSubscription:
    """Opt in to a URI, an async application handler, and explicit recovery policy."""

    uri: str
    handler: Callable[[MCPResourceEvent], Awaitable[None]] = field(repr=False)
    method: Literal["resources/subscribe", "subscriptions/listen"] = "resources/subscribe"
    read_on_start: bool = True
    read_on_reconnect: bool = True
    handler_timeout_seconds: float = 30

    def __post_init__(self) -> None:
        """Reject unusable listeners at authoring time instead of silently ignoring them."""

        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("MCP subscription URI must be non-empty")
        if not inspect.iscoroutinefunction(self.handler):
            raise TypeError("MCP subscription handler must be async")
        if self.method not in {"resources/subscribe", "subscriptions/listen"}:
            raise ValueError("unsupported MCP subscription method")
        if type(self.handler_timeout_seconds) not in (int, float):
            raise TypeError("MCP subscription handler timeout must be numeric")
        if not math.isfinite(self.handler_timeout_seconds) or self.handler_timeout_seconds <= 0:
            raise ValueError("MCP subscription handler timeout must be positive")
        if type(self.read_on_start) is not bool or type(self.read_on_reconnect) is not bool:
            raise TypeError("MCP subscription recovery options must be booleans")


class SubscriptionLifecycle(MCPClientLifecycle):
    """Start listeners only with the runtime; CLI inspection never starts this owner."""

    def __init__(self, configured: Any) -> None:
        self.configured = configured
        self.workers: list[_ResourceListener] = []

    async def start(self, context: Any) -> None:
        """Treat initial subscription acknowledgment and retained reads as startup work."""

        try:
            for subscription in self.configured.subscriptions:
                worker = _ResourceListener(self.configured, subscription, context.framework)
                self.workers.append(worker)
                await worker.start()
        except BaseException:
            await self.close(context)
            raise

    async def close(self, context: Any) -> None:
        """Cancel streams before the owning HTTP credential lifecycle is released."""

        workers, self.workers = self.workers, []
        await asyncio.gather(*(worker.close() for worker in workers))


class _ResourceListener:
    """Serialize handlers and coalesce notifications into reads of the latest value."""

    def __init__(self, configured: Any, subscription: MCPSubscription, framework: str) -> None:
        self.configured = configured
        self.subscription = subscription
        self.framework = framework
        self.ready = asyncio.get_running_loop().create_future()
        self.task: asyncio.Task | None = None
        self.digest: str | None = None
        self.dirty = asyncio.Event()
        self.stopping = False

    async def start(self) -> None:
        """Do not inherit a request's principal, session, credentials, or context lease."""

        self.task = Context().run(asyncio.create_task, self._run())
        try:
            await asyncio.wait_for(
                asyncio.shield(self.ready),
                self.configured.timeout_seconds + self.subscription.handler_timeout_seconds,
            )
        except BaseException:
            await self.close()
            if self.ready.done() and not self.ready.cancelled():
                self.ready.exception()
            raise

    async def close(self) -> None:
        """Cancellation closes transport scopes inside their original owning task."""

        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if not self.ready.done():
            self.ready.cancel()

    async def _run(self) -> None:
        """Fail initial startup clearly; reconnect established listeners with bounded backoff."""

        delay = 1.0
        while not self.stopping:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.ready.done():
                    self.ready.set_exception(MCPResourceError("MCP subscription startup failed"))
                    return
            # AnyIO can combine cancellation with transport cleanup errors.
            # Shutdown intent must survive that transformation into an ExceptionGroup.
            if self.stopping:
                return
            self._audit("reconnecting")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _listen(self) -> None:
        """Keep protocol selection explicit; never silently downgrade subscription semantics."""

        if self.subscription.method == "subscriptions/listen":
            from .mcp_subscription_http import listen

            await listen(self)
            return
        async with resource_session(
            self.configured, self.framework, message_handler=self._notification,
        ) as (session, initialized):
            resources = initialized.capabilities.resources
            if resources is None or not resources.subscribe:
                raise MCPResourceError("MCP server does not support resource subscriptions")
            await session.subscribe_resource(self.subscription.uri)
            read = lambda: session.read_resource(self.subscription.uri)
            await self.connected(read)
            while True:
                # SDK 1.x has no public disconnect waiter. A bounded idle probe
                # detects clean transport loss as well as exceptional closure.
                try:
                    await asyncio.wait_for(self.dirty.wait(), min(30, self.configured.sse_read_timeout_seconds))
                except asyncio.TimeoutError:
                    pass
                self.dirty.clear()
                await self.deliver(read, "update")

    async def _notification(self, message: Any) -> None:
        """Coalesce matching resource invalidations without buffering arbitrary provider data."""

        from mcp.types import ResourceUpdatedNotification

        if isinstance(message, Exception):
            self.dirty.set()
            return
        notification = getattr(message, "root", None)
        if isinstance(notification, ResourceUpdatedNotification) and str(notification.params.uri) == self.subscription.uri:
            self.dirty.set()

    async def connected(self, read: Any) -> None:
        """Read only after acknowledgment to close the subscribe/read race."""

        initial = not self.ready.done()
        recover = self.subscription.read_on_start if initial else self.subscription.read_on_reconnect
        if recover:
            await self.deliver(read, "start" if initial else "reconnect")
        if initial:
            self.ready.set_result(None)

    async def deliver(self, read: Any, reason: str) -> None:
        """Commit deduplication only after a successful handler; recovery may deliver again."""

        result = await read()
        resource = result if isinstance(result, dict) else _bounded_result(result, self.configured.max_content_bytes)
        serialized = json.dumps(resource.get("contents", []), sort_keys=True).encode()
        if len(serialized) > self.configured.max_content_bytes:
            raise MCPResourceError("MCP resource exceeds max_content_bytes")
        digest = hashlib.sha256(serialized).hexdigest()
        if digest == self.digest:
            return
        event = MCPResourceEvent(self.configured._client_name(), self.subscription.uri, resource, reason)
        try:
            await asyncio.wait_for(self.subscription.handler(event), self.subscription.handler_timeout_seconds)
        except BaseException:
            self._audit("failed")
            raise
        self.digest = digest
        self._audit("committed")

    def _audit(self, outcome: str) -> None:
        """Record delivery outcomes without resource URIs, payloads, or handler error text."""

        from .logging import get_logger

        get_logger("mcp.audit").info(
            "MCP subscription delivery", operation="mcp.subscription",
            trigger="subscription", outcome=outcome,
        )
