"""Declarative, versioned Studio Pack contracts and bounded source snapshots."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
import yaml

from .files import LIMIT, read, source_path, validate

STUDIO_VERSION = "0.1.0"
MAX_PACK_BYTES = 8 * LIMIT


class Contract(BaseModel):
    """Reject misspelled configuration instead of silently ignoring company policy."""
    model_config = ConfigDict(extra="forbid", strict=True)


class Compatibility(Contract):
    """Keep Studio and compiler compatibility explicit and independently versioned."""
    studio: str = ">=0.1,<0.2"
    harnest: str = ">=1.5,<2"


class Resource(Contract):
    """Map native project destinations to inert, pack-relative text artifacts."""
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    kind: Literal["component", "extension", "template", "agent-skill", "builder-skill", "profile", "service", "bundle"]
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    files: dict[str, str] = Field(default_factory=dict, max_length=64)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    includes: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_shape(self):
        """Separate executable contributions, composition, and builder-only guidance."""
        self._valid_composition()
        if self.config and self.kind != "profile":
            raise ValueError("Only profiles can contribute a configuration overlay")
        if self.kind == "builder-skill" and any(not path.endswith(".md") for path in self.files):
            raise ValueError("Builder skills contain Markdown guidance only")
        return self

    def _valid_composition(self):
        """Keep bundle references distinct from native source contributions."""
        if self.kind == "bundle":
            if not self.includes or self.files or self.config:
                raise ValueError("Bundles require includes and cannot contain files or configuration")
        elif self.includes or not (self.files or self.config):
            raise ValueError("Resources require files or configuration; only bundles can include resources")


class Manifest(Contract):
    """One portable organization contribution pack, independent of deployment."""
    apiVersion: Literal["harnest.dev/studio-pack/v1"]
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    version: str
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    requires: Compatibility = Field(default_factory=Compatibility)
    resources: list[Resource] = Field(min_length=1, max_length=100)


def runtime_version() -> str:
    """Use the installed compiler's public version for compatibility checks."""
    from importlib.metadata import version
    return version("harnest")


def load_pack(root: Path) -> dict:
    """Freeze only explicitly referenced text; never import a pack or follow links."""
    if root.is_symlink():
        raise ValueError("Linked pack roots are excluded")
    root = root.expanduser().resolve()
    manifest = Manifest.model_validate(yaml.safe_load(read(root, "studio-pack.yaml")["text"]))
    Version(manifest.version)
    if Version(STUDIO_VERSION) not in SpecifierSet(manifest.requires.studio):
        raise ValueError(f"{manifest.name} requires Studio {manifest.requires.studio}")
    if Version(runtime_version()) not in SpecifierSet(manifest.requires.harnest):
        raise ValueError(f"{manifest.name} requires Harnest {manifest.requires.harnest}")
    entries, total = {}, 0
    for resource in manifest.resources:
        if resource.id in entries:
            raise ValueError("Duplicate resource id: " + resource.id)
        texts = _resource_files(root, resource)
        total += sum(len(text.encode()) for text in texts.values())
        if total > MAX_PACK_BYTES:
            raise ValueError("Studio packs are limited to 8 MiB of source")
        payload = {**resource.model_dump(), "files": texts}
        payload["digest"] = digest(payload)
        entries[resource.id] = payload
    pack = {"manifest": manifest.model_dump(), "resources": entries}
    pack["digest"] = digest(pack)
    return pack


def _resource_files(root: Path, resource: Resource) -> dict:
    """Apply the native source path and syntax rules before exposing a contribution."""
    files = {}
    for destination, relative in resource.files.items():
        if destination == "studio-packs.lock":
            raise ValueError("studio-packs.lock is reserved for installation receipts")
        text = read(root, relative)["text"]
        if not source_path(root, relative).is_file():
            raise ValueError("Missing pack source: " + relative)
        validate(source_path(root, destination), text)
        files[destination] = text
    return files


def digest(value) -> str:
    """Hash canonical resource content, including configuration and composition."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text_digest(text: str) -> str:
    """Use the same UTF-8 digest as revision-checked workspace edits."""
    return hashlib.sha256(text.encode()).hexdigest()
