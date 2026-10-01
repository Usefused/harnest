"""Validated MCP review inputs and deterministic, revision-bound source proposals."""

import hashlib
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from .files import read, source_path, validate


class ServiceSelection(BaseModel):
    """Require explicit operation scope instead of treating empty selections as all access."""
    model_config = ConfigDict(extra="forbid", strict=True)
    slug: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    operations: list[str] = Field(default_factory=list, max_length=200)
    select_all: bool = False

    @model_validator(mode="after")
    def scope(self):
        """All operations and a finite allowlist are mutually exclusive review choices."""
        if self.select_all == bool(self.operations) or len(set(self.operations)) != len(self.operations):
            raise ValueError("Choose specific operations or explicitly select all operations")
        return self


class MCPPlan(BaseModel):
    """Use the same contract for manual forms and model-proposed plans."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    kind: Literal["http", "existing", "create"]
    resource: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    owner: str = "agent.py"
    deployment_agent: str = ""
    name: str = Field(default="", max_length=63)
    version: str = Field(default="1.0.0", max_length=64)
    server_offset: int = Field(default=0, ge=0, le=10000)
    description: str = Field(default="", max_length=512)
    bucket: str = Field(default="default", min_length=1, max_length=128)
    owner_team: str | None = Field(default=None, max_length=128)
    services: list[ServiceSelection] = Field(default_factory=list, max_length=20)
    url: str = Field(default="", max_length=2048)
    bearer: str = Field(default="", max_length=8192, exclude=True, repr=False)


def endpoint(value):
    """Allow HTTP transports without inline credentials, fragments, or query-string secrets."""
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise HTTPException(422, "Use a valid HTTP(S) MCP endpoint.") from None
    if any(ord(char) < 32 for char in value):
        raise HTTPException(422, "The endpoint contains control characters.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "Use an HTTP(S) MCP endpoint without credentials, query parameters, or fragments.")
    return value


def configuration(plan):
    """Render only reviewed fields into a full Fused configuration."""
    if not plan.name or not plan.services:
        raise HTTPException(422, "Name the server and select workspace services.")
    if len({item.slug for item in plan.services}) != len(plan.services):
        raise HTTPException(422, "Each service may appear once.")
    return {"apiVersion": "fused/v1", "kind": "mcp", "name": plan.name, "version": plan.version,
            "description": plan.description or plan.name, "bucket": plan.bucket,
            "services": {item.slug: {"version": item.version, **({"select_all": True} if item.select_all else {"operations": item.operations})} for item in plan.services}}


def validate_services(service, session, plan):
    """Reject invented service versions and operation IDs against authenticated discovery."""
    available = {(item["slug"], item["version"]): item for item in service.services(session)["items"]}
    for selected in plan.services:
        found = available.get((selected.slug, selected.version))
        if found is None:
            raise HTTPException(422, "A selected service version is not available in this workspace.")
        operations = service.operations(session, found["id"], selected.version)["items"]
        if set(selected.operations) - {item["id"] for item in operations}:
            raise HTTPException(422, "A selected operation was not returned by Fused discovery.")


def source_changes(root, plan, authenticated):
    """Generate one owner-scoped MCP factory and update existing deployment bindings."""
    owner = source_path(root, plan.owner)
    if owner.name != "agent.py" or not owner.is_file():
        raise HTTPException(422, "Choose an existing agent.py as the MCP owner.")
    relative = (PurePosixPath(plan.owner).parent / "mcp" / (plan.resource + ".py")).as_posix()
    digest = hashlib.sha256(relative.encode()).hexdigest()[:8].upper()
    prefix = "HARNEST_MCP_" + plan.resource.upper() + "_" + digest
    url_env, token_env = prefix + "_URL", prefix + "_TOKEN"
    headers = f',\n        headers={{"Authorization": "Bearer ${{{token_env}}}"}}' if authenticated else ""
    text = ('from harnest.mcp import MCPClient\n\n\ndef client() -> MCPClient:\n'
            '    """Connect using credentials supplied by the trusted launch environment."""\n'
            f'    return MCPClient.streamable_http(\n        "${{{url_env}}}"{headers},\n    )\n')
    files = [change(root, relative, text)]
    deploy = read(root, "harnest-deployment.yaml")
    if deploy["revision"]:
        files.append(deployment_change(root, deploy, [url_env, token_env] if authenticated else [url_env], plan.deployment_agent))
    return files, url_env, token_env


def change(root, path, text):
    """Bind generated text to a complete source snapshot before approval."""
    current = read(root, path)
    validate(source_path(root, path), text)
    return {**current, "before": current["text"], "text": text}


def deployment_change(root, document, variables, selected):
    """Preserve deployment services and bind credentials only on this source project's agents."""
    validate(source_path(root, document["path"]), document["text"])
    value = yaml.safe_load(document["text"])
    from harnest.provisioner_config import parse_manifest
    parsed = parse_manifest(document["text"])
    matched = [selected] if selected in parsed.agents else list(parsed.agents) if len(parsed.agents) == 1 and not selected else []
    if not matched:
        raise HTTPException(422, "Choose the deployment agent name to receive these MCP bindings.")
    for key in matched:
        environment = value["agents"][key].setdefault("environment", {})
        for name in variables:
            environment[name] = {"secret": name}
    return change(root, document["path"], yaml.safe_dump(value, sort_keys=False))
