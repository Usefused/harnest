"""Opt-in crash recovery against databases created and owned by this test only."""

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid

from harnest.memory import MemoryRecord, MemoryScope
from harnest_postgres import PostgresMemoryStore
from harnest_redis import RedisMemoryStore


def command(*arguments):
    """Bound process lifetime and capture only test-owned service diagnostics."""
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(f'{arguments[0]} failed: {result.stderr}')
    return result.stdout.strip()


def free_port():
    """Choose an ephemeral loopback port for a test-owned database."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


@unittest.skipUnless(os.environ.get('HARNEST_TEST_MEMORY_CRASH') == '1', 'requires explicit disposable-database crash opt-in')
class MemoryCrashLiveTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, make_store, restart):
        """Verify acknowledged data survives server death, not merely client replacement."""
        scope = MemoryScope('crash-test-' + uuid.uuid4().hex, 'synthetic-user')
        record = MemoryRecord(scope, 'code', 'synthetic recovery value', {'empty': [], 'large': 2**60 + 1})
        store = make_store()
        await store.start()
        try:
            saved = await store.put(record)
            restart()
        finally:
            await store.close()
        recovered = make_store()
        await recovered.start()
        try:
            self.assertEqual(await recovered.get(scope, saved.key), saved)
            self.assertTrue(await recovered.delete(scope, saved.key, expected_revision=saved.revision))
            self.assertIsNone(await recovered.get(scope, saved.key))
        finally:
            await recovered.close()

    async def test_postgres_immediate_crash_recovers_acknowledged_write(self):
        """Crash only a freshly initialized temporary PostgreSQL cluster."""
        initdb, pg_ctl = shutil.which('initdb'), shutil.which('pg_ctl')
        self.assertTrue(initdb and pg_ctl, 'PostgreSQL binaries must be on PATH')
        with tempfile.TemporaryDirectory(prefix='harnest-memory-crash-') as directory:
            data, log = str(Path(directory) / 'data'), str(Path(directory) / 'server.log')
            port = free_port()
            command(initdb, '-D', data, '-U', 'postgres', '-A', 'trust', '--no-locale')
            options = f'-h 127.0.0.1 -p {port} -k {directory}'
            command(pg_ctl, '-D', data, '-l', log, '-o', options, '-w', 'start')
            try:
                def restart():
                    """Immediate shutdown requires WAL recovery on the next start."""
                    command(pg_ctl, '-D', data, '-m', 'immediate', '-w', 'stop')
                    command(pg_ctl, '-D', data, '-l', log, '-o', options, '-w', 'start')
                await self.exercise(lambda: PostgresMemoryStore(f'postgresql://postgres@127.0.0.1:{port}/postgres'), restart)
            finally:
                command(pg_ctl, '-D', data, '-m', 'fast', '-w', 'stop')

    async def test_redis_sigkill_recovers_acknowledged_write(self):
        """Kill only a newly created container with synchronous append-only persistence."""
        port = free_port()
        container = command('docker', 'run', '--detach', '--name', 'harnest-memory-crash-' + uuid.uuid4().hex,
                            '-p', f'127.0.0.1:{port}:6379', 'valkey/valkey:9.0.0',
                            'valkey-server', '--appendonly', 'yes', '--appendfsync', 'always')
        try:
            command('docker', 'exec', container, 'valkey-cli', 'ping')
            def restart():
                """Preserve this container's volume while simulating abrupt process loss."""
                command('docker', 'kill', '--signal', 'KILL', container)
                command('docker', 'start', container)
                command('docker', 'exec', container, 'valkey-cli', 'ping')
            await self.exercise(lambda: RedisMemoryStore(f'redis://127.0.0.1:{port}/0'), restart)
        finally:
            command('docker', 'stop', container)
            command('docker', 'rm', '-v', container)
