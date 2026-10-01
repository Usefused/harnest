"""Reviewable transport configuration using the runtime's canonical validation."""

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
import yaml

from harnest.server_config import _UniqueKeyLoader, _project_server_document, _decode_config, ServerConfigError
from .files import read, source_path


class TransportChoice(BaseModel):
    """Select the transport without accepting arbitrary source paths or YAML."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    agui: bool


def settings(root: Path) -> dict:
    """Respect a legacy server file instead of silently creating conflicting inline policy."""
    document = read(root, "config.yaml")
    value = _mapping(document["text"])
    legacy = source_path(root, "server.yaml")
    if legacy.is_file():
        if "server" in value:
            raise HTTPException(422, "Move legacy server.yaml settings into config.yaml before editing transports.")
        document = read(root, "server.yaml")
        value = _mapping(document["text"])
        policy = value
    else:
        policy = value.get("server", {})
    if not isinstance(policy, dict):
        raise HTTPException(422, "Server settings must be a YAML mapping.")
    return {"document": document, "agui": policy.get("agui", True)}


def proposal(root: Path, enabled: bool) -> dict:
    """Change only the selected scalar, preserving comments and concurrent-edit protection."""
    current = settings(root)
    document = current["document"]
    text = _updated(document["text"], enabled, inline=document["path"] == "config.yaml")
    value = _mapping(text)
    try:
        compiled = _project_server_document(value["server"]) if document["path"] == "config.yaml" else value
        _decode_config(compiled, Path(document["path"]), None)
    except ServerConfigError:
        raise HTTPException(422, "Fix server settings before changing AG-UI.") from None
    return {"summary": f"{'Enable' if enabled else 'Disable'} AG-UI at POST /agui. Rebuild and restart the agent to apply the transport setting.",
            "files": [{**document, "before": document["text"], "text": text}]}


def _mapping(text: str) -> dict:
    """Reject duplicate keys and malformed source rather than guessing which value wins."""
    try:
        value = yaml.load(text, Loader=_UniqueKeyLoader)
        if not isinstance(value, dict):
            raise ValueError("Expected a mapping")
        return value
    except (ValueError, yaml.YAMLError):
        raise HTTPException(422, "Fix configuration YAML before editing transports.") from None


def _updated(text: str, enabled: bool, *, inline: bool) -> str:
    """Use parser marks to retain untouched authored text and environment references."""
    if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(text)):
        raise HTTPException(422, "Edit transport settings in source when the YAML uses anchors or aliases.")
    root = yaml.compose(text)
    setting = "true" if enabled else "false"
    if not inline:
        return _scalar(text, root, setting)
    server = next((value for key, value in root.value if key.value == "server"), None)
    if server is not None:
        return _scalar(text, server, setting)
    return _insert(text, root, "server", "{agui: " + setting + "}")


def _scalar(text: str, mapping, value: str) -> str:
    """Replace one scalar or add it inside an existing block or flow mapping."""
    node = next((item for key, item in mapping.value if key.value == "agui"), None)
    if node is not None:
        if not isinstance(node, yaml.ScalarNode):
            raise HTTPException(422, "AG-UI must be a boolean or an environment reference.")
        return text[:node.start_mark.index] + value + text[node.end_mark.index:]
    return _insert(text, mapping, "agui", value)


def _insert(text: str, mapping, key: str, value: str) -> str:
    """Preserve block indentation and flow delimiters without serializing unrelated settings."""
    offset = mapping.start_mark.index
    if mapping.flow_style:
        addition = f"{key}: {value}" + (", " if mapping.value else "")
        offset += 1
    else:
        addition = f"{key}: {value}\n" + " " * mapping.start_mark.column
    return text[:offset] + addition + text[offset:]


def install_routes(app, workspace):
    """Expose configuration previews; the existing revision-checked apply route owns writes."""
    @app.get("/api/transports")
    def current(project: str):
        """Read the selected project's explicit AG-UI setting without resolving secrets."""
        with workspace.lock:
            result = settings(workspace.project(project))
            return {"agui": result["agui"], "path": result["document"]["path"]}

    @app.post("/api/transports/preview")
    def preview(body: TransportChoice):
        """Prepare an exact source diff for the user to review before applying."""
        with workspace.lock:
            return proposal(workspace.project(body.project), body.agui)
