"""Bounded, self-contained OpenAPI documents for runtime MCP bridges."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml

MAX_SPEC_BYTES = 16 * 1024 * 1024


def load_spec(source: str) -> dict[str, Any]:
    """Fetch only the explicitly requested document and freeze it as JSON data."""

    if urlsplit(source).scheme in {"https", "http"}:
        payload = _download(source)
    else:
        with Path(source).open("rb") as stream:
            payload = stream.read(MAX_SPEC_BYTES + 1)
    if len(payload) > MAX_SPEC_BYTES:
        raise ValueError("OpenAPI document exceeds 16 MiB")
    try:
        document = yaml.safe_load(payload)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        document = None
    validate_spec(document)
    # A JSON round trip rejects YAML-only values and cycles before writing files.
    try:
        normalized = json.loads(json.dumps(document, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        normalized = None
    if normalized is None:
        raise ValueError("OpenAPI document must contain finite, acyclic JSON data")
    return normalized


def _download(source: str) -> bytes:
    """Bound downloads and avoid forwarding credentials through redirects."""

    import httpx

    validate_url(source)
    chunks = []
    size = 0
    failure = None
    try:
        with httpx.stream("GET", source, timeout=30, follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_SPEC_BYTES:
                    raise ValueError("OpenAPI document exceeds 16 MiB")
                chunks.append(chunk)
    except httpx.HTTPError:
        failure = ValueError("could not download OpenAPI document; check the URL and HTTP status")
    if failure is not None:
        raise failure
    return b"".join(chunks)


def validate_url(value: str) -> str:
    """Require explicit HTTP authority without embedded credentials or templates."""

    if not isinstance(value, str):
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment or "{" in value:
        raise ValueError("URLs cannot contain credentials, fragments, or unresolved server variables")
    return value


def validate_spec(document: Any) -> None:
    """Reject unsupported source versions and runtime reference fetching."""

    if not isinstance(document, dict):
        raise ValueError("OpenAPI document must be a JSON or YAML object")
    version = document.get("openapi", "")
    if not isinstance(version, str) or not version.startswith(("3.0.", "3.1.")):
        raise ValueError("local MCP conversion requires OpenAPI 3.0 or 3.1")
    if not isinstance(document.get("paths"), dict) or not document["paths"]:
        raise ValueError("OpenAPI document must define non-empty paths")
    _validate_references(document)


def _validate_references(document: dict[str, Any]) -> None:
    """Bound traversal and require external references to be bundled in advance."""

    pending: list[Any] = [document]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list)) or id(value) in seen:
            continue
        seen.add(id(value))
        if len(seen) > 100_000:
            raise ValueError("OpenAPI document exceeds the structure limit")
        if isinstance(value, dict):
            _local_reference(value.get("$ref"))
            pending.extend(value.values())
        else:
            pending.extend(value)


def _local_reference(reference: Any) -> None:
    """Keep conversion independent of runtime files and network references."""

    if reference is not None and (not isinstance(reference, str) or not reference.startswith("#/")):
        raise ValueError("bundle external OpenAPI references into one document before creating the catalog")


def api_base_url(document: dict[str, Any], source: str, override: str | None) -> str:
    """Choose one API origin explicitly when the source cannot supply it."""

    if override is not None:
        return validate_url(override)
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        raise ValueError("spec has no server URL; supply base_url to MCPClient.from_openapi")
    url = servers[0].get("url")
    if not isinstance(url, str):
        raise ValueError("spec has no server URL; supply base_url to MCPClient.from_openapi")
    if urlsplit(source).scheme in {"http", "https"}:
        url = urljoin(source, url)
    return validate_url(url)
