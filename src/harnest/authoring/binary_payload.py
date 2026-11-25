"""Validate and stage pack payloads for standalone branded Harnest CLI binaries.

A standalone pack binary embeds the pack folder plus validated coding-IDE skills,
bundled Python wheels (or local Python packages built into wheels), pinned PEP 508
requirements, and canonical Harnest Extensions in a single deterministic directory
ready to be appended to a native Harnest binary.
"""
from __future__ import annotations

import email.parser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from zipfile import BadZipFile, ZipFile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import yaml

from ..extension_descriptors import (
    EXTENSION_CONTRIBUTION_KINDS,
    ExtensionConventionError,
    ExtensionDescriptor,
    discover_extensions,
)
from .contracts import ProjectError
from .distribution import _pack_files
from .packs import Pack, _compiler_owned_distributions, _looks_like_pack_path, check_studio, load_pack

BINARY_FORMAT = "harnest.pack-binary/v1"
BINARY_MANIFEST = "harnest-pack-binary.json"
_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MAX_WHEEL_BYTES = 128 * 1024 * 1024
_SKIPPED_SOURCE_PARTS = frozenset({
    ".git", ".hg", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".DS_Store",
})


def validate_pack_payload(pack: Pack) -> None:
    """Validate coding skills, bundled packages, and extensions without executing code."""
    _validate_skills(pack)
    _partition_packages(pack)
    _validate_extensions(pack)


def prepare_pack_binary(
    root: str | Path,
    staging: str | Path,
    *,
    uv_executable: str | None = None,
) -> dict[str, Any]:
    """Validate one pack and stage its complete standalone binary payload tree."""
    pack = load_pack(root)
    # Fail before staging when the Studio sub-pack or embedded capabilities are invalid.
    check_studio(pack)
    validate_pack_payload(pack)
    target = Path(staging).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    _stage_pack_folder(pack, target)
    skill_records = _stage_skills(pack, target)
    local_packages, requirements = _partition_packages(pack)
    wheel_records = _stage_wheels(local_packages, target, uv_executable=uv_executable)
    extension_records = _stage_extensions(pack, target)
    manifest = _build_binary_manifest(
        pack, target, skill_records, wheel_records, requirements, extension_records,
    )
    (target / BINARY_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _resolve_contained_path(root: Path, relative: str, label: str) -> Path:
    """Resolve one pack-relative path while rejecting symlinks and escapes."""
    candidate = root / relative
    # Check lexical ancestors so a symlinked directory cannot pull external files in.
    for ancestor in _lexical_ancestors(root, relative):
        if ancestor.is_symlink():
            raise ProjectError(f"{label} cannot traverse a symlink: {relative}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ProjectError(f"{label} escapes the pack directory: {relative}")
    return candidate


def _lexical_ancestors(root: Path, relative: str) -> list[Path]:
    """Return every intermediate path from root down to root/relative."""
    current = root
    ancestors: list[Path] = []
    for part in Path(relative).parts:
        current = current / part
        ancestors.append(current)
    return ancestors


def _validate_skills(pack: Pack) -> list[tuple[str, Path]]:
    """Validate every declared coding-IDE skill folder and its SKILL.md frontmatter."""
    seen: set[str] = set()
    entries: list[tuple[str, Path]] = []
    for relative in pack.manifest.skills:
        directory = _resolve_contained_path(pack.root, relative, "skill")
        if not directory.is_dir():
            raise ProjectError(f"pack skill must be a directory: {relative}")
        name = directory.name
        if not _SKILL_NAME.fullmatch(name):
            raise ProjectError(f"pack skill directory name must be kebab-case: {relative}")
        if name in seen:
            raise ProjectError(f"duplicate pack skill name: {name}")
        _validate_skill_tree(directory, name, relative)
        seen.add(name)
        entries.append((name, directory))
    return entries


def _validate_skill_tree(directory: Path, expected_name: str, label: str) -> None:
    """Verify SKILL.md frontmatter identity and reject links or special files."""
    manifest_file = directory / "SKILL.md"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ProjectError(f"pack skill {label} must contain SKILL.md")
    _check_skill_frontmatter(manifest_file, expected_name, label)
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise ProjectError(f"pack skill {label} cannot contain symlinks: {item.name}")
        if not item.is_dir() and not item.is_file():
            raise ProjectError(f"pack skill {label} must contain regular files only: {item.name}")


def _check_skill_frontmatter(path: Path, expected_name: str, label: str) -> None:
    """Require valid YAML frontmatter naming the skill directory."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ProjectError(f"pack skill {label}/SKILL.md must start with YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ProjectError(f"pack skill {label}/SKILL.md has unterminated YAML frontmatter")
    loaded = yaml.safe_load(text[4:end])
    if not isinstance(loaded, dict):
        raise ProjectError(f"pack skill {label}/SKILL.md frontmatter must be a mapping")
    if loaded.get("name") != expected_name:
        raise ProjectError(
            f"pack skill {label}/SKILL.md frontmatter name must match directory {expected_name!r}"
        )


def _is_bare_requirement(entry: str) -> bool:
    """Return True when a PEP 508 entry is a plain name with no version, extras, or markers."""
    parsed = Requirement(entry)
    return not parsed.specifier and not parsed.extras and parsed.marker is None


def _validate_local_package_entry(pack: Pack, relative: str, owned: frozenset[str]) -> Path:
    """Validate that one local packages entry is a contained .whl file or pyproject.toml directory."""
    candidate = _resolve_contained_path(pack.root, relative, "packages")
    # Accept prebuilt wheels directly after verifying their distribution metadata.
    if candidate.is_file() and candidate.suffix == ".whl":
        dist_name = _wheel_distribution_name(candidate)
        _reject_compiler_owned(dist_name, relative, owned)
        return candidate
    # Accept local Python package directories that can be built into wheels at pack build time.
    if candidate.is_dir() and (candidate / "pyproject.toml").is_file():
        _validate_local_package_tree(candidate, relative, owned)
        return candidate
    raise ProjectError(
        f"packages entry must be a .whl file or Python package directory with pyproject.toml: {relative}"
    )


def _partition_packages(pack: Pack) -> tuple[list[Path], list[str]]:
    """Classify unified pack.yaml packages entries into local wheel/package sources and index requirements."""
    owned = _compiler_owned_distributions()
    local_sources: list[Path] = []
    requirements: list[str] = []
    for entry in pack.manifest.packages:
        # Explicit path entries (containing /, .whl, etc.) must resolve to a local wheel or package folder.
        if _looks_like_pack_path(entry):
            local_sources.append(_validate_local_package_entry(pack, entry, owned))
            continue
        # A bare single-segment name that exists at the pack root refers to a local package directory.
        if _is_bare_requirement(entry):
            probe = pack.root / entry
            if probe.is_symlink() or probe.exists():
                local_sources.append(_validate_local_package_entry(pack, entry, owned))
                continue
        requirements.append(entry)
    # Keep bundled wheel payloads bounded to prevent oversized single-file binaries.
    if len(local_sources) > 64:
        raise ProjectError("pack may bundle at most 64 local wheels or package directories")
    return local_sources, requirements


def _validate_local_package_tree(directory: Path, label: str, owned: frozenset[str]) -> None:
    """Reject symlinks and compiler-owned package identities before wheel builds."""
    import tomllib

    project_file = directory / "pyproject.toml"
    if project_file.is_symlink():
        raise ProjectError(f"local package pyproject.toml cannot be a symlink: {label}")
    document = tomllib.loads(project_file.read_text(encoding="utf-8"))
    project = document.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        raise ProjectError(f"local package {label}/pyproject.toml must declare [project].name")
    _reject_compiler_owned(canonicalize_name(project["name"]), label, owned)


def _reject_compiler_owned(name: str, label: str, owned: frozenset[str]) -> None:
    """Keep packs from shadowing Harnest or framework SDK distributions."""
    if name in owned:
        raise ProjectError(
            f"pack package {label} must not replace compiler-owned distribution {name!r}"
        )


def _wheel_distribution_name(wheel_path: Path) -> str:
    """Read the canonical PEP 503 distribution name from a wheel's METADATA file."""
    if wheel_path.stat().st_size > _MAX_WHEEL_BYTES:
        raise ProjectError(f"wheel exceeds 128 MiB limit: {wheel_path.name}")
    try:
        with ZipFile(wheel_path, "r") as archive:
            metadata_paths = [
                name for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(metadata_paths) != 1:
                raise ProjectError(f"invalid wheel metadata in {wheel_path.name}")
            raw = archive.read(metadata_paths[0]).decode("utf-8")
    except (BadZipFile, UnicodeDecodeError, OSError) as exc:
        raise ProjectError(f"invalid wheel archive {wheel_path.name}: {exc}") from exc
    parsed = email.parser.Parser().parsestr(raw)
    name = parsed.get("Name")
    if not name or not name.strip():
        raise ProjectError(f"wheel {wheel_path.name} is missing Name in METADATA")
    return canonicalize_name(name)


def _validate_extensions(pack: Pack) -> list[tuple[Path, ExtensionDescriptor]]:
    """Validate every pack extension using Harnest's canonical extension descriptor rules."""
    if not pack.manifest.extensions:
        return []
    seen: set[str] = set()
    resolved: list[tuple[Path, ExtensionDescriptor]] = []
    with tempfile.TemporaryDirectory(prefix="harnest-pack-ext-check-") as temp:
        check_root = Path(temp)
        for relative in pack.manifest.extensions:
            source = _resolve_contained_path(pack.root, relative, "extension")
            if not source.is_dir():
                raise ProjectError(f"pack extension must be a directory: {relative}")
            if source.name.casefold() in seen:
                raise ProjectError(f"duplicate pack extension name: {source.name}")
            _copy_regular_tree(source, check_root / source.name, f"extension {relative}")
            seen.add(source.name.casefold())
        try:
            descriptors = discover_extensions(check_root)
        except ExtensionConventionError as exc:
            raise ProjectError(f"invalid pack extension: {exc}") from exc
        by_name = {item.name: item for item in descriptors}
        for relative in pack.manifest.extensions:
            source = _resolve_contained_path(pack.root, relative, "extension")
            descriptor = by_name.get(source.name)
            if descriptor is None:
                raise ProjectError(f"pack extension {relative} is missing extension.yaml")
            _validate_extension_root_entries(source, descriptor, relative)
            resolved.append((source, descriptor))
    return resolved


def _validate_extension_root_entries(
    source: Path, descriptor: ExtensionDescriptor, label: str,
) -> None:
    """Reject unexpected extension files before embedding them into the pack binary."""
    allowed = {"README.md", "extension.yaml", "extension.py", "pyproject.toml", "lib"}
    for kind in EXTENSION_CONTRIBUTION_KINDS:
        for path in descriptor.contribution_paths(kind):
            allowed.add(path.relative_to(descriptor.directory).parts[0])
    for item in sorted(source.iterdir(), key=lambda entry: entry.name):
        if item.name in _SKIPPED_SOURCE_PARTS:
            continue
        if item.name not in allowed:
            raise ProjectError(f"pack extension {label} contains unexpected entry: {item.name}")


def _stage_pack_folder(pack: Pack, staging: Path) -> None:
    """Copy the inert pack folder into staging/pack using the standard wheel exclusions."""
    files = _pack_files(pack.root, staging)
    pack_target = staging / "pack"
    pack_target.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        destination = pack_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def _stage_skills(pack: Pack, staging: Path) -> list[dict[str, str]]:
    """Copy validated coding-IDE skills into staging/skills/<name>."""
    records: list[dict[str, str]] = []
    for name, directory in _validate_skills(pack):
        destination = staging / "skills" / name
        _copy_regular_tree(directory, destination, f"skill {name}")
        records.append({"name": name, "root": f"skills/{name}"})
    return records


def _stage_wheels(
    local_packages: list[Path], staging: Path, *, uv_executable: str | None,
) -> list[dict[str, str]]:
    """Copy prebuilt wheels and build local Python package folders into staging/wheels."""
    owned = _compiler_owned_distributions()
    records: list[dict[str, str]] = []
    seen_distributions: set[str] = set()
    seen_filenames: set[str] = set()
    wheels_dir = staging / "wheels"
    for source in local_packages:
        wheels_dir.mkdir(parents=True, exist_ok=True)
        staged = _materialize_wheel(source, wheels_dir, uv_executable=uv_executable)
        dist_name = _wheel_distribution_name(staged)
        _reject_compiler_owned(dist_name, source.name, owned)
        if dist_name in seen_distributions:
            raise ProjectError(f"duplicate bundled pack package distribution: {dist_name}")
        if staged.name in seen_filenames:
            raise ProjectError(f"duplicate bundled wheel filename: {staged.name}")
        seen_distributions.add(dist_name)
        seen_filenames.add(staged.name)
        records.append({
            "name": dist_name,
            "filename": f"wheels/{staged.name}",
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        })
    return records


def _materialize_wheel(
    source: Path, wheels_dir: Path, *, uv_executable: str | None,
) -> Path:
    """Copy a prebuilt wheel or build one wheel from a local Python package directory."""
    if source.is_file():
        target = wheels_dir / source.name
        shutil.copy2(source, target)
        return target
    with tempfile.TemporaryDirectory(prefix="harnest-pack-wheel-") as built_dir:
        out = Path(built_dir)
        _build_local_wheel(source, out, uv_executable=uv_executable)
        built = sorted(out.glob("*.whl"))
        if len(built) != 1:
            raise ProjectError(
                f"building local package {source.name} expected 1 wheel, produced {len(built)}"
            )
        target = wheels_dir / built[0].name
        shutil.copy2(built[0], target)
        return target


def _build_local_wheel(source: Path, out: Path, *, uv_executable: str | None) -> None:
    """Build a wheel from a local package directory using uv, python -m build, or pip."""
    commands = _wheel_build_commands(source, out, uv_executable)
    errors: list[str] = []
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        errors.append(f"{command[0]}: {(result.stderr or result.stdout).strip()}")
    raise ProjectError(
        f"failed to build wheel for local package {source.name}: {'; '.join(errors)}"
    )


def _wheel_build_commands(
    source: Path, out: Path, uv_executable: str | None,
) -> list[list[str]]:
    """Order available wheel builders so offline and embedded-uv hosts succeed first."""
    commands: list[list[str]] = []
    # Prefer embedded or PATH uv because released Harnest binaries always bundle uv.
    uv = uv_executable or shutil.which("uv")
    if uv:
        commands.append([uv, "build", "--wheel", "--out-dir", str(out), str(source)])
    commands.append([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(source)])
    commands.append([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(out), str(source)])
    return commands


def _stage_extensions(pack: Pack, staging: Path) -> list[dict[str, Any]]:
    """Copy validated canonical Harnest Extensions into staging/extensions/<name>."""
    records: list[dict[str, Any]] = []
    for source, descriptor in _validate_extensions(pack):
        destination = staging / "extensions" / descriptor.name
        _copy_regular_tree(source, destination, f"extension {descriptor.name}")
        records.append({
            "name": descriptor.name,
            "path": f"extensions/{descriptor.name}",
            "hasPyproject": (destination / "pyproject.toml").is_file(),
        })
    return records


def _copy_regular_tree(source: Path, destination: Path, label: str) -> None:
    """Copy a directory tree while refusing symlinks and skipping local cache clutter."""
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if any(part in _SKIPPED_SOURCE_PARTS for part in relative.parts) or item.suffix == ".pyc":
            continue
        if item.is_symlink():
            raise ProjectError(f"{label} cannot contain symlinks: {relative.as_posix()}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file():
            raise ProjectError(f"{label} must contain regular files only: {relative.as_posix()}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _build_binary_manifest(
    pack: Pack,
    staging: Path,
    skills: list[dict[str, str]],
    wheels: list[dict[str, str]],
    requirements: list[str],
    extensions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the canonical pack binary manifest with a complete payload digest."""
    commands = [
        {"name": cmd.name, "help": cmd.help}
        for cmd in (pack.project.commands if pack.project else ())
    ]
    protected = sorted({
        *(item["name"] for item in wheels),
        *(canonicalize_name(Requirement(req).name) for req in requirements),
    })
    manifest: dict[str, Any] = {
        "format": BINARY_FORMAT,
        "name": pack.name,
        "version": pack.manifest.version,
        "title": pack.manifest.title,
        "description": pack.manifest.description,
        "command": pack.manifest.command_name,
        "hasProjectHooks": pack.project is not None,
        "commands": commands,
        "skills": skills,
        "wheels": wheels,
        "requirements": requirements,
        "protectedPackages": protected,
        "extensions": extensions,
    }
    manifest["digest"] = _payload_digest(staging, manifest)
    return manifest


def _payload_digest(staging: Path, manifest: dict[str, Any]) -> str:
    """Hash manifest metadata and every staged file in deterministic order."""
    hasher = hashlib.sha256()
    hasher.update(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for item in sorted(staging.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(staging).as_posix()
        if relative == BINARY_MANIFEST:
            continue
        hasher.update(b"\x00" + relative.encode("utf-8") + b"\x00")
        hasher.update(item.read_bytes())
    return hasher.hexdigest()


__all__ = ["BINARY_FORMAT", "BINARY_MANIFEST", "prepare_pack_binary", "validate_pack_payload"]
