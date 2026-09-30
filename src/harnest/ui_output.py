"""Bounded UI event delivery shared by tools, graph nodes, and child scopes."""

import asyncio
from contextlib import suppress
import json
import re

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_RESERVED = {"agent_activity", "agent_metadata", "decision_result", "thinking"}


class UIOutput:
    """Own transient display events, never model transcript or persistent state."""
    def __init__(self):
        """Allocate one buffer per invocation; child contexts share the same budget."""
        self._buffer = []
        self._queue = None
        self._count = 0
        self._bytes = 0

    async def emit(self, name, value, *, agent):
        """Validate and detach JSON before yielding authority to an asynchronous consumer."""
        event, size = _event(name, value, agent)
        if self._count >= 128 or self._bytes + size > 512 * 1024:
            raise ValueError("UI event budget exceeded")
        self._count += 1
        self._bytes += size
        if self._queue is None:
            self._buffer.append(event)
        else:
            await self._queue.put(("event", event))

    def drain(self):
        """Transfer buffered events once for non-streaming or short-circuited invocations."""
        events, self._buffer = self._buffer, []
        return events

    async def stream(self, iterator):
        """Run the backend in one scoped task so UI events can arrive during a long tool call."""
        self._queue = asyncio.Queue(maxsize=1)
        # One producer keeps native generator ContextVar tokens on their original
        # task; creating a separate task per anext would violate that ownership.
        producer = asyncio.create_task(self._produce(iterator))
        try:
            for event in self.drain():
                yield event
            while True:
                kind, value = await self._queue.get()
                if kind == "end":
                    return
                if kind == "error":
                    raise value
                if kind == "native":
                    event, consumed = value
                    yield event
                    consumed.set_result(None)
                else:
                    yield value
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
            self._queue = None

    async def _produce(self, iterator):
        """Serialize native events and propagate failures while owning generator cleanup."""
        try:
            async for event in iterator:
                consumed = asyncio.get_running_loop().create_future()
                await self._queue.put(("native", (event, consumed)))
                # Preserve native iterator backpressure and lifecycle ordering.
                await consumed
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            await self._queue.put(("error", error))
        else:
            await self._queue.put(("end", None))
        finally:
            closer = getattr(iterator, "aclose", None)
            if callable(closer):
                await closer()


def _event(name, value, agent):
    """Disallow reserved control names, non-JSON values, and unbounded display payloads."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("UI event name must use 1–128 letters, numbers, dots, underscores, or hyphens")
    if name.startswith("harnest.") or name in _RESERVED:
        raise ValueError("UI event name is reserved by Harnest")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        if size > 65536:
            raise ValueError("too large")
        detached = json.loads(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("UI event value must be finite JSON of at most 64 KiB") from None
    return {"type":"ui_event", "name":name, "value":detached, "agent":agent}, size
