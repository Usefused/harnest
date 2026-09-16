"""Explicit developer provisioning through Fused's existing CLI contracts."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
from pathlib import Path
import tempfile
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from harnest.mcp import MCPClient

from ._cli import CLI, FusedCLIError, required_string

if TYPE_CHECKING:
    from .client import FusedMCPClient, OpenAPISpec


@dataclass(frozen=True)
class FusedSetupResult:
    """A provisioned standard client and non-secret setup artifacts.

    The CLI hands its one-time token directly to the operator. Store it in the
    environment variable named by ``token_env`` before connecting this client.
    """

    client: MCPClient
    url: str
    url_env: str
    token_env: str
    config_path: Path
    receipt_directory: Path

    def write_client(self, path: str | Path) -> Path:
        """Create a standard client factory without overwriting authored code."""

        from ._serialize import client_source

        source = client_source(self.client)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as output:
            output.write(source)
        return target


@dataclass(frozen=True)
class _Import:
    """Keep each source tied to its own exact review receipt and provider version."""

    spec: OpenAPISpec
    receipt: Path
    plan_id: str
    version: str


def setup(
    client: FusedMCPClient, *, description: str, bucket: str, version: str,
    project: str | Path, cli: str, timeout_seconds: float,
) -> FusedSetupResult:
    """Plan all imports, commit them, then provision exactly one MCP version.

    There is no rollback or automatic replay: imported services may survive a
    later failure. Unique retained receipts support the CLI's recovery workflow.
    """

    _validate_setup(client, description, bucket, version)
    project = Path(project).resolve(strict=True)
    runner = CLI.create(cli, project, timeout_seconds)
    sources = [_source_arguments(spec, project) for spec in client.specs]
    parent = project / ".fused" / "harnest" / client.name
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="setup-", dir=parent))
    # Check the effective saved-login/environment identity, never switch keys.
    runner.run("identity check", ["whoami"])
    imports = [_plan_import(runner, spec, source, directory)
               for spec, source in zip(client.specs, sources)]
    configuration = _configuration(client.name, description, bucket, version, imports)
    config_path = directory / "mcp.json"
    config_path.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")
    for item in imports:
        _apply_import(runner, item)
    receipt = directory / "mcp.plan.json"
    _plan_mcp(runner, config_path, receipt)
    # MCP apply has no JSON contract. Preserve its human token handoff; obtain
    # the endpoint separately through the structured exact-version catalogue.
    runner.run("MCP apply", ["mcp", "apply", "--file", str(config_path),
                             "--receipt", str(receipt)], structured=False)
    url = _version_url(runner, client.name, version)
    values = {item.name: getattr(client, item.name) for item in fields(MCPClient) if item.init}
    values["url"] = url
    return FusedSetupResult(MCPClient(**values), url, client.url_env, client.token_env,
                            config_path, directory)


def _validate_setup(client: FusedMCPClient, *metadata: str) -> None:
    """Reject incomplete application metadata before any provisioning operation."""

    if not client.specs:
        raise ValueError("setup requires a from_openapi declaration")
    if any(not isinstance(value, str) or not value.strip() for value in metadata):
        raise ValueError("description, bucket, and app version must be non-empty strings")


def _source_arguments(spec: OpenAPISpec, project: Path) -> list[str]:
    """Resolve local paths once while delegating source parsing to Fused."""

    source = str(spec.source)
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("OpenAPI URLs must have a hostname and no embedded credentials")
        return ["--url", source]
    path = (project / source).resolve(strict=True)
    if not path.is_file():
        raise ValueError("OpenAPI source must be a file")
    return [str(path)]


def _plan_import(runner: CLI, spec: OpenAPISpec, source: list[str], directory: Path) -> _Import:
    """Preserve one receipt per spec so a later plan cannot replace its review."""

    receipt = directory / f"{spec.name}.import.plan.json"
    arguments = ["import", "plan", *source, "--name", spec.name, "--slug", spec.name,
                 "--target", "endpoints", "--strict", "--receipt-out", str(receipt)]
    if spec.version is not None:
        arguments.extend(["--version", spec.version])
    result = runner.run(f"import plan ({spec.name})", arguments)
    if not isinstance(result, dict) or result.get("slug") != spec.name:
        raise FusedCLIError("import plan returned a mismatched service identity")
    plan_id = required_string(result, "plan_id", "import plan")
    required_string(result, "review_hash", "import plan")
    version = required_string(result, "target_version", "import plan")
    return _Import(spec, receipt, plan_id, version)


def _apply_import(runner: CLI, item: _Import) -> None:
    """Require a committed result for the exact plan before provisioning its MCP."""

    result = runner.run(f"import apply ({item.spec.name})",
                        ["import", "apply", "--receipt", str(item.receipt)])
    expected = {"status": "applied", "phase": "complete", "commit_state": "committed",
                "operation_id": item.plan_id, "slug": item.spec.name, "version": item.version}
    if not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items()):
        raise FusedCLIError("import apply outcome is unknown; inspect the retained receipt with fused-cli import status")
    required_string(result, "service_id", "import apply")
    required_string(result, "service_version_id", "import apply")
    # Successful import apply already activates this exact version in Engine.
    # A second workspace apply could mirror away unrelated services.


def _configuration(name: str, description: str, bucket: str, version: str,
                   imports: list[_Import]) -> dict[str, Any]:
    """Keep operation selection per service while producing one MCP config."""

    return {"apiVersion": "fused/v1", "kind": "mcp", "name": name,
            "version": version, "description": description, "bucket": bucket,
            "services": {item.spec.name: item.spec.selection(item.version) for item in imports}}


def _plan_mcp(runner: CLI, configuration: Path, receipt: Path) -> None:
    """Let Engine validate operation IDs, bucket access, and immutable scope."""

    result = runner.run("MCP plan", ["mcp", "plan", "--file", str(configuration),
                                    "--receipt-out", str(receipt)])
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise FusedCLIError("MCP plan must return exactly one plan")
    required_string(result[0], "plan_id", "MCP plan")


def _version_url(runner: CLI, name: str, version: str) -> str:
    """Page the server's versions and bind only the requested immutable version."""

    offset = 0
    while True:
        page = runner.run("MCP version lookup", ["mcp", "versions", name,
                           "--limit", "100", "--offset", str(offset)])
        items, total = _version_page(page, offset)
        matches = [item for item in items if item.get("name") == name and item.get("version") == version]
        if matches:
            return _endpoint(matches)
        offset += len(items)
        if offset >= total:
            raise FusedCLIError("provisioned MCP version was not found; inspect setup receipts")


def _version_page(page: Any, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Fail on malformed or stalled pagination instead of silently selecting latest."""

    if not isinstance(page, dict):
        raise FusedCLIError("invalid MCP version page")
    items, total = page.get("items"), page.get("total")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise FusedCLIError("invalid MCP version items")
    if type(total) is not int or total < 0 or (not items and offset < total):
        raise FusedCLIError("invalid MCP version pagination")
    return items, total


def _endpoint(matches: list[dict[str, Any]]) -> str:
    """Use Engine's pinned HTTP endpoint, never synthesize a route or follow latest."""

    if len(matches) != 1 or matches[0].get("status") != "active":
        raise FusedCLIError("MCP version is ambiguous or inactive")
    urls = matches[0].get("transport_urls")
    if not isinstance(urls, dict):
        raise FusedCLIError("MCP version omitted transport_urls")
    url = required_string(urls, "versioned_streamable_http", "MCP version")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise FusedCLIError("MCP version returned an invalid HTTP endpoint")
    return url
