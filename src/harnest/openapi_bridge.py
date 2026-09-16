"""Convert an OpenAPI document when a standard MCP stdio connection starts."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from .openapi_spec import api_base_url, load_spec

_SOURCE = "_HARNEST_OPENAPI_SOURCE"
_BASE = "_HARNEST_OPENAPI_BASE_URL"
_HEADERS = "_HARNEST_OPENAPI_HEADERS"


def connection_environment(
    source: str | Path, base_url: str | None, headers: Mapping[str, str] | None,
) -> dict[str, str]:
    """Declare a bridge without reading specs, resolving secrets, or starting it."""

    if not isinstance(source, (str, Path)) or not str(source).strip():
        raise ValueError("OpenAPI source must be a non-empty path or HTTP(S) URL")
    if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
        raise ValueError("OpenAPI base_url must be a non-empty URL")
    return {_SOURCE: str(source), _BASE: base_url or "", **_header_environment(headers)}


def _header_environment(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Keep header values separate from serialized metadata and process arguments."""

    if headers is not None and not isinstance(headers, Mapping):
        raise TypeError("OpenAPI headers must be a mapping")
    environment = {}
    names = {}
    for index, (header, value) in enumerate((headers or {}).items()):
        if not isinstance(header, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header):
            raise ValueError("OpenAPI headers must use valid HTTP header names")
        if not isinstance(value, str):
            raise TypeError("OpenAPI header values must be strings")
        variable = f"_HARNEST_OPENAPI_HEADER_{index}"
        names[header] = variable
        # Expand each credential as a standalone value. JSON interpolation would
        # corrupt credentials containing quotes or backslashes at connection time.
        environment[variable] = value
    environment[_HEADERS] = json.dumps(names)
    return environment


def runtime_configuration() -> tuple[dict[str, Any], str, dict[str, str]]:
    """Load a fresh spec for this connection, separate from provider credentials."""

    source = os.environ[_SOURCE]
    document = load_spec(source)
    base = api_base_url(document, source, os.environ.get(_BASE) or None)
    names = json.loads(os.environ[_HEADERS])
    headers = {name: os.environ[variable] for name, variable in names.items()}
    return document, base, headers


async def serve() -> None:
    """Own conversion and the HTTP client for the MCP connection lifetime."""

    import httpx
    try:
        from fastmcp import FastMCP
    except ImportError:
        raise RuntimeError("OpenAPI bridges require harnest[openapi]; sync the agent environment") from None

    document, base, headers = await asyncio.to_thread(runtime_configuration)
    async with httpx.AsyncClient(base_url=base, headers=headers,
                                timeout=30, follow_redirects=False) as client:
        server = FastMCP.from_openapi(openapi_spec=document, client=client,
                                     name="Harnest OpenAPI")
        await server.run_async(transport="stdio", show_banner=False)


def main() -> int:
    """Keep stdout reserved for MCP and avoid exposing credential-bearing errors."""

    try:
        asyncio.run(serve())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"OpenAPI bridge startup failed ({type(error).__name__}); check source, environment variables, and harnest[openapi] installation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
