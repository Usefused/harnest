"""Runtime OpenAPI declaration, transport, credentials, and bundle portability."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from harnest.bundle import _copy_agent_source, _discover_mcp
from harnest.mcp import MCPClient
from harnest.openapi_spec import load_spec


def spec(base="https://api.example.test/v1"):
    """A parameterized endpoint proves the converter executes real HTTP calls."""

    return {"openapi": "3.0.3", "info": {"title": "Catalog", "version": "1.0"},
            "servers": [{"url": base}], "paths": {"/items/{item_id}": {"get": {
                "operationId": "get_item", "summary": "Read an item",
                "parameters": [{"name": "item_id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Item", "content": {"application/json": {
                    "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}}}}}}}}



class OpenAPITests(unittest.TestCase):
    def test_declaration_is_offline_and_preserves_mcp_options(self):
        with patch('harnest.openapi_bridge.load_spec', side_effect=AssertionError('no I/O')):
            client = MCPClient.from_openapi('https://api.test/spec.json',
                                           headers={'Authorization': 'Bearer ${TOKEN}'},
                                           tools=['get_item'], prefix='crm',
                                           permission='crm.connect', timeout_seconds=45)
        self.assertEqual(client.transport, 'stdio')
        self.assertEqual(client.tool_filter, ['get_item'])
        self.assertEqual(client.tool_name_prefix, 'crm')
        self.assertEqual(client.permission, 'crm.connect')
        self.assertEqual(client.timeout_seconds, 45)
        self.assertIn('Bearer ${TOKEN}', client.env.values())
        self.assertNotIn('fused-cli', (client.command, *client.args))

    def test_source_paths_resolve_against_relocated_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'agent'
            (project / 'mcp').mkdir(parents=True)
            (project / 'mcp/_api.json').write_text(json.dumps(spec()))
            (project / 'mcp/api.py').write_text(
                'from pathlib import Path\nfrom harnest.mcp import MCPClient\n'
                'def client():\n'
                '    return MCPClient.from_openapi(Path(__file__).with_name("_api.json"))\n')
            destination = root / 'compiled' / 'source'
            destination.parent.mkdir()
            _copy_agent_source(project, destination)
            shutil.rmtree(project)
            configured, = _discover_mcp(destination / 'mcp')
            path = Path(configured.env['_HARNEST_OPENAPI_SOURCE'])
            self.assertTrue(path.is_relative_to(destination))
            self.assertEqual(load_spec(str(path)), spec())

    def test_invalid_declarations_fail_without_starting_a_bridge(self):
        for options in ({'source': ''}, {'source': []}, {'source': 'a.json', 'base_url': ''},
                        {'source': 'a.json', 'headers': {'Bad\nHeader': 'value'}}):
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                MCPClient.from_openapi(**options)

    def test_unsupported_documents_and_external_refs_are_rejected_at_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'spec.json'
            for update in ({'openapi': '3.2.0'}, {'components': {'schemas': {'Remote': {'$ref': 'https://remote.test/schema'}}}}):
                document = spec()
                document.update(update)
                source.write_text(json.dumps(document))
                client = MCPClient.from_openapi(source)
                from harnest.openapi_bridge import runtime_configuration
                with patch.dict(os.environ, client.env), self.assertRaises(ValueError):
                    runtime_configuration()

    def test_explicit_base_and_quoted_credentials_survive_runtime_resolution(self):
        from harnest.openapi_bridge import runtime_configuration
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'spec.json'
            source.write_text(json.dumps(spec('https://{tenant}.example.test')))
            client = MCPClient.from_openapi(source, base_url='https://tenant.example.test',
                                           headers={'X-Key': '${TOKEN}'})
            token = 'quoted"and\\escaped'
            with patch.dict(os.environ, TOKEN=token):
                connection = client.to_langgraph_connection()
            with patch.dict(os.environ, connection['env']):
                _, base, headers = runtime_configuration()
            self.assertEqual(base, 'https://tenant.example.test')
            self.assertEqual(headers, {'X-Key': token})


class _API(BaseHTTPRequestHandler):
    """A local provider used only by the real bridge round trip."""

    def do_GET(self):
        """Record credentials and a path parameter, then return valid API JSON."""

        self.server.observed.append((self.path, self.headers.get('Authorization')))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        payload = spec('/v1') if self.path == '/openapi.json' else {'id': self.path.rsplit('/', 1)[-1]}
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):
        """Keep provider request data out of test diagnostics."""


class OpenAPITransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_fetches_spec_then_calls_api_with_scoped_credentials(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = ThreadingHTTPServer(('127.0.0.1', 0), _API)
        server.observed = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = MCPClient.from_openapi(f'http://127.0.0.1:{server.server_port}/openapi.json',
                                           headers={'Authorization': 'Bearer ${CATALOG_TOKEN}'})
            self.assertEqual(server.observed, [])
            # Reconnecting must reload the spec, without caching generated files.
            for token in ('first-token', 'second-token'):
                with patch.dict(os.environ, CATALOG_TOKEN=token):
                    options = client.to_langgraph_connection()
                parameters = StdioServerParameters(command=options['command'], args=options['args'], env=options['env'])
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listing = await session.list_tools()
                        self.assertEqual([tool.name for tool in listing.tools], ['get_item'])
                        response = await session.call_tool('get_item', {'item_id': '123'})
                        self.assertFalse(response.isError, response)
                        self.assertIn('123', response.content[0].text)
            self.assertEqual(server.observed, [('/openapi.json', None), ('/v1/items/123', 'Bearer first-token'),
                                              ('/openapi.json', None), ('/v1/items/123', 'Bearer second-token')])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
