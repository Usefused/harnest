"""Inspect installed extension manifests without importing or executing extension code."""

import yaml

from .files import inventory, read


def installed(root) -> list[dict]:
    """Keep malformed manifests visible and editable instead of hiding broken extensions."""
    files = inventory(root)
    manifests = [path for path in files if path.startswith("extensions/") and path.count("/") == 2 and path.endswith("/extension.yaml")]
    return [describe(root, path, files) for path in manifests]


def describe(root, path: str, files: list[str]) -> dict:
    """Read declarative metadata only; contribution code is opened through the source editor."""
    prefix = path.rsplit("/", 1)[0] + "/"
    item = {"name": prefix.split("/")[1], "version": "", "capabilities": [], "files": [file for file in files if file.startswith(prefix)], "manifest": path, "error": ""}
    try:
        data = yaml.safe_load(read(root, path)["text"])
        metadata = data.get("metadata", {})
        item.update(name=str(metadata.get("name", item["name"])), version=str(metadata.get("version", "")))
        capabilities = data.get("capabilities", [])
        item["capabilities"] = [str(value) for value in capabilities] if isinstance(capabilities, list) else []
    except (yaml.YAMLError, AttributeError, TypeError):
        item["error"] = "Invalid manifest. Open configuration to repair it."
    return item


def install_routes(app, workspace) -> None:
    """Expose installed packages within an explicitly selected project."""
    @app.get("/api/extensions")
    def extensions(project: str):
        """Refresh package inventory directly from disk after CLI installs or external edits."""
        with workspace.lock:
            return {"extensions": installed(workspace.project(project))}
