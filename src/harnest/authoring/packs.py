"""Load one Harnest pack: an inert pack.yaml plus optional trusted pack.py hooks.

A pack is a folder. ``pack.yaml`` declares identity, compatibility, the branded
command and an optional ``studio`` sub-pack, and can be read without running
code. ``pack.py`` defines ``pack = ProjectPack(...)`` with init, migration and
command hooks, and is imported only when the caller asked for this pack.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from importlib.metadata import PackageNotFoundError, version as installed_version
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Literal, Mapping, Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import ProjectError, ProjectPack, validate_pack_name
from .context import yaml_mapping

MANIFEST = "pack.yaml"
PROJECT_MODULE = "pack.py"
API_VERSION = "harnest.dev/pack/v1"
_MAX_MANIFEST_BYTES = 256 * 1024
_LEGACY_STUDIO = "studio-pack.yaml"


class _Strict(BaseModel):
    """Reject misspelled keys instead of silently ignoring company policy."""
    model_config = ConfigDict(extra="forbid", strict=True)


class PackRequirements(_Strict):
    """Version ranges checked before any pack code runs."""
    harnest: str | None = None
    studio: str | None = None

    @field_validator("harnest", "studio")
    @classmethod
    def _specifier(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                SpecifierSet(value)
            except InvalidSpecifier as exc:
                raise ValueError(f"invalid version range: {value!r}") from exc
        return value


class PackManifest(_Strict):
    """The inert pack.yaml contract; the ``studio`` section is validated by Studio."""
    apiVersion: Literal["harnest.dev/pack/v1"]
    name: str
    version: str
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    command: str | None = None
    requires: PackRequirements = Field(default_factory=PackRequirements)
    studio: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_pack_name(value)

    @field_validator("command")
    @classmethod
    def _command(cls, value: str | None) -> str | None:
        if value is not None:
            validate_pack_name(value)
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        try:
            return str(Version(value))
        except InvalidVersion as exc:
            raise ValueError(f"invalid pack version: {value!r}") from exc

    @property
    def command_name(self) -> str:
        """The branded executable name; defaults to the pack name."""
        return self.command or self.name


@dataclass(frozen=True)
class Pack:
    """One loaded pack. ``project`` is None when the pack has no pack.py."""
    root: Path
    manifest: PackManifest
    project: ProjectPack | None

    @property
    def name(self) -> str:
        """The pack name from pack.yaml; also its lock key."""
        return self.manifest.name

    @property
    def studio(self) -> Mapping[str, Any] | None:
        """The raw `studio:` section, validated by Studio when it loads the pack."""
        return self.manifest.studio


def read_manifest(root: str | Path) -> tuple[Path, PackManifest]:
    """Validate pack.yaml and its version ranges without importing anything."""
    path = Path(root).expanduser()
    if path.is_symlink():
        raise ProjectError(f"pack folder cannot be a symlink: {path}")
    path = path.resolve()
    if not path.is_dir():
        raise ProjectError(f"pack folder not found: {path}")
    manifest_path = path / MANIFEST
    _reject_legacy(path, manifest_path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProjectError(f"{manifest_path}: pack.yaml must be a regular file")
    content = manifest_path.read_bytes()
    if len(content) > _MAX_MANIFEST_BYTES:
        raise ProjectError(f"{manifest_path}: pack.yaml exceeds 256 KiB")
    try:
        manifest = PackManifest.model_validate(yaml_mapping(content, MANIFEST))
    except ValidationError as exc:
        fields = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'pack.yaml'}: {e['msg']}"
                           for e in exc.errors(include_input=False))
        raise ProjectError(f"{manifest_path}: {fields}") from exc
    _check_harnest(manifest)
    return path, manifest


def load_pack(root: str | Path, *, import_project: bool = True) -> Pack:
    """Load a pack folder; pack.py runs only when ``import_project`` is true."""
    path, manifest = read_manifest(root)
    project = _import_project(path, manifest.name) if import_project else None
    return Pack(path, manifest, project)


def load_packs(roots: Sequence[str | Path]) -> tuple[Pack, ...]:
    """Load several packs in the caller's order and reject duplicate names."""
    packs = tuple(load_pack(root) for root in roots)
    names = [pack.name for pack in packs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ProjectError(f"duplicate packs: {', '.join(duplicates)}")
    return packs


def check_studio(pack: Pack) -> str:
    """Validate the studio sub-pack with the installed Studio, without importing pack code.

    Returns a short status. Raises ProjectError when the sub-pack is invalid.
    Without Studio installed nothing can be checked here; `harnest studio
    --pack` checks it when Studio starts.
    """
    if pack.studio is None:
        return "none"
    try:
        from harnest_builder.packs import Packs
        from harnest_builder.ui_packs import UIPacks
    except ModuleNotFoundError:
        return "present (not checked: Studio is not installed here)"
    try:
        catalog = Packs([pack.root])
        UIPacks(catalog, trusted=catalog.packs).catalog()
    except Exception as exc:  # Studio reports HTTPException, ValueError and OSError.
        raise ProjectError(f"{pack.name}: invalid studio sub-pack: {getattr(exc, 'detail', exc)}") from exc
    loaded = catalog.packs[pack.name]
    return f"valid ({len(loaded['resources'])} resources{', UI' if loaded['manifest'].get('ui') else ''})"


def parse_options(values: Sequence[str], packs: Sequence[Pack]) -> dict[str, dict[str, Any]]:
    """Turn repeated ``--option key=value`` flags into per-pack option values.

    With one pack, ``key=value`` targets it. With several packs, use
    ``pack.key=value``. Values starting with ``[`` or ``{`` are read as JSON;
    everything else stays a string for the pack's typed model to validate.
    """
    names = [pack.name for pack in packs]
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise ProjectError(f"--option must be key=value: {item!r}")
        owner, dot, field = key.partition(".")
        if dot:
            if owner not in names:
                raise ProjectError(f"--option {key}: no pack named {owner!r}")
        elif len(names) == 1:
            owner, field = names[0], key
        else:
            raise ProjectError(f"--option {key}: prefix it with a pack name when using several packs")
        value: Any = raw
        if raw[:1] in {"[", "{"}:
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise ProjectError(f"--option {key}: invalid JSON value") from exc
        result.setdefault(owner, {})[field.replace("-", "_")] = value
    return result


def _reject_legacy(root: Path, manifest_path: Path) -> None:
    """Point authors of the removed formats at the merged layout."""
    if (root / _LEGACY_STUDIO).exists():
        raise ProjectError(
            f"{root}: studio-pack.yaml is no longer supported. Move its contents under a "
            "`studio:` section in pack.yaml (apiVersion harnest.dev/pack/v1); name, version, "
            "title, description and requires stay at the top level.")
    if not manifest_path.exists() and (root / PROJECT_MODULE).exists():
        raise ProjectError(
            f"{root}: pack.yaml is required. Packs are no longer wrapped in ProjectCLI; "
            "declare name and version in pack.yaml and keep `pack = ProjectPack(...)` in pack.py.")


def _check_harnest(manifest: PackManifest) -> None:
    """Refuse a pack written for a different Harnest before running its code."""
    required = manifest.requires.harnest
    if required is None:
        return
    try:
        current = installed_version("harnest")
    except PackageNotFoundError:
        return
    if Version(current) not in SpecifierSet(required, prereleases=True):
        raise ProjectError(f"{manifest.name} requires Harnest {required}; installed {current}")


def _import_project(root: Path, name: str) -> ProjectPack | None:
    """Import trusted pack.py with its folder importable for local helper modules."""
    module_path = root / PROJECT_MODULE
    if not module_path.exists():
        return None
    if module_path.is_symlink() or not module_path.is_file():
        raise ProjectError(f"{module_path}: pack.py must be a regular file")
    module_name = "harnest_pack_" + name.replace("-", "_")
    module = _load_project_module(module_path, module_name)
    project = getattr(module, "pack", None)
    if not isinstance(project, ProjectPack):
        raise ProjectError(f"{module_path}: define `pack = ProjectPack(schema_version=...)`")
    return project.bind(name, root)


def _load_project_module(module_path: Path, module_name: str) -> ModuleType:
    """Execute trusted hooks with local imports and restore the temporary search path."""
    root = module_path.parent
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ProjectError(f"{module_path}: cannot import pack.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError(f"{module_path}: import failed ({type(exc).__name__}: {exc})") from exc
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    return module


__all__ = [
    "API_VERSION", "MANIFEST", "PROJECT_MODULE", "Pack", "PackManifest", "PackRequirements",
    "check_studio", "load_pack", "load_packs", "parse_options", "read_manifest",
]
