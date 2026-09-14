"""Loopback-only proxy that drops one real database acknowledgement."""

import asyncio
from urllib.parse import urlsplit, urlunsplit


class LostReplyProxy:
    """Forward traffic unchanged until explicitly armed to discard a reply."""

    def __init__(self, url):
        self.target = urlsplit(url)
        if self.target.hostname not in {'127.0.0.1', 'localhost'} or self.target.scheme != 'redis':
            raise ValueError('fault proxy requires a plain loopback Redis test URL')
        self.drop_reply = False
        self.connections = set()
        self.server = None

    async def start(self):
        """Expose a private ephemeral loopback listener preserving credentials."""
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        credentials = self.target.netloc.rsplit('@', 1)[0] + '@' if '@' in self.target.netloc else ''
        return urlunsplit(self.target._replace(netloc=f'{credentials}127.0.0.1:{port}'))

    async def close(self):
        """Join proxy handlers so tests never leak sockets or background tasks."""
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)

    async def accept(self, reader, writer):
        """Own both directions and close them together after an injected failure."""
        task = asyncio.current_task()
        self.connections.add(task)
        upstream = None
        pumps = []
        try:
            remote, upstream = await asyncio.open_connection(self.target.hostname, self.target.port or 6379)
            pumps = [asyncio.create_task(self.pipe(reader, upstream, False)),
                     asyncio.create_task(self.pipe(remote, writer, True))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            await writer.wait_closed()
            self.connections.discard(task)

    async def pipe(self, reader, writer, response):
        """Drop only after the real server has replied, proving commit ambiguity."""
        while data := await reader.read(65536):
            if response and self.drop_reply:
                self.drop_reply = False
                return
            writer.write(data)
            await writer.drain()
