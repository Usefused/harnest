"""Build a pack into a wheel whose console script is the pack's branded command.

The wheel holds the pack folder and a stdlib-only launcher. ``acme init``,
``acme upgrade`` and ``acme studio`` become the Harnest command with
``--pack <installed pack>``; the pack's own commands run through
``harnest pack run``; anything else is forwarded to Harnest unchanged.
"""
from __future__ import annotations

import base64
import csv
import hashlib
from io import StringIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .contracts import ProjectError
from .packs import Pack, check_studio, load_pack

# Build output, caches and environments never belong in a published pack.
_SKIPPED = frozenset({
    ".git", ".hg", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "node_modules", "dist", "build", ".DS_Store",
})
_MAX_FILES = 2048
_MAX_BYTES = 64 * 1024 * 1024
_EPOCH = (1980, 1, 1, 0, 0, 0)


def distribution_name(pack: Pack) -> str:
    """Mirror harnest-extension-* and harnest-template-* naming."""
    return f"harnest-pack-{pack.name}"


def build_pack(root: str | Path, output: str | Path = "dist") -> Path:
    """Validate the pack, import its hooks to list commands, and write one wheel."""
    pack = load_pack(root)
    # A broken studio sub-pack must fail here, not on a teammate's machine.
    check_studio(pack)
    destination = Path(output).expanduser().resolve()
    files = _pack_files(pack.root, destination)
    module = "harnest_pack_" + pack.name.replace("-", "_")
    distribution = distribution_name(pack)
    version = pack.manifest.version
    commands = tuple(command.name for command in pack.project.commands) if pack.project else ()
    contents: dict[str, bytes] = {f"{module}/pack/{relative}": data for relative, data in files.items()}
    contents[f"{module}/__init__.py"] = f'"""Installed {pack.name} Harnest pack."""\n'.encode()
    contents[f"{module}/launcher.py"] = _launcher(pack, commands).encode()
    metadata = f"{distribution.replace('-', '_')}-{version}.dist-info"
    summary = pack.manifest.title.replace("\n", " ")
    contents[f"{metadata}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
        f"Summary: {summary}\nRequires-Python: >=3.11\n").encode()
    contents[f"{metadata}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: harnest\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    contents[f"{metadata}/entry_points.txt"] = (
        f"[console_scripts]\n{pack.manifest.command_name} = {module}.launcher:main\n").encode()
    contents[f"{metadata}/RECORD"] = _record(contents, metadata)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    with target.open("wb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for path, content in sorted(contents.items()):
            # Fixed timestamps keep rebuilds of the same pack byte-identical.
            info = ZipInfo(path, date_time=_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return target


def _pack_files(root: Path, output: Path) -> dict[str, bytes]:
    """Collect regular files, refusing links that could pull in content from elsewhere."""
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _excluded_pack_path(relative, path, output):
            continue
        if path.is_symlink():
            raise ProjectError(f"pack files cannot be symlinks: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProjectError(f"pack files must be regular files: {relative.as_posix()}")
        data = path.read_bytes()
        total += len(data)
        files[relative.as_posix()] = data
        if len(files) > _MAX_FILES or total > _MAX_BYTES:
            raise ProjectError("packs are limited to 2048 files and 64 MiB")
    return files


def _excluded_pack_path(relative: Path, path: Path, output: Path) -> bool:
    """Exclude local state, credentials and build output before inspecting file types."""
    return (
        any(part in _SKIPPED for part in relative.parts)
        or path.suffix == ".pyc"
        or path.name == ".env"
        or path.name.startswith(".env.")
        or path == output
        or output in path.parents
    )


def _launcher(pack: Pack, commands: tuple[str, ...]) -> str:
    """Generate the stdlib-only branded command."""
    name = pack.manifest.command_name
    custom = "\n".join(f"  {command:<14} {_help(pack, command)}" for command in commands)
    usage = (f"usage: {name} COMMAND [ARGS]\n\n{pack.manifest.title}\n\n"
             "Pack commands:\n"
             f"  {'init':<14} create an agent with this pack\n"
             f"  {'upgrade':<14} plan or apply Harnest and pack migrations\n"
             f"  {'studio':<14} open Harnest Studio with this pack\n"
             + (custom + "\n" if custom else "")
             + "\nAny other command is passed to Harnest unchanged.\n")
    return f'''"""Branded command for the {pack.name} Harnest pack (generated by harnest pack build)."""
import os
import subprocess
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent / "pack"
NAME = {name!r}
CUSTOM = frozenset({sorted(commands)!r})
PACK_AWARE = frozenset({{"init", "upgrade", "studio"}})
USAGE = {usage!r}


def main() -> int:
    """Route the command to Harnest with this pack attached."""
    harnest = os.environ.get("HARNEST_CLI", "harnest")
    arguments = sys.argv[1:]
    first = arguments[0] if arguments else ""
    if first in {{"", "-h", "--help", "help"}}:
        print(USAGE, end="")
        return 0
    if first in PACK_AWARE:
        command = [harnest, first, *arguments[1:], "--pack", str(PACK)]
    elif first in CUSTOM:
        command = [harnest, "pack", "run", str(PACK), *arguments]
    else:
        command = [harnest, *arguments]
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print(f"{{NAME}}: Harnest CLI not found ({{harnest}}); install Harnest or set HARNEST_CLI", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _help(pack: Pack, command: str) -> str:
    assert pack.project is not None
    for item in pack.project.commands:
        if item.name == command:
            return item.help
    return ""


def _record(contents: dict[str, bytes], metadata: str) -> bytes:
    """Write wheel RECORD hashes in the standard URL-safe SHA-256 form."""
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for path, content in sorted(contents.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([path, "sha256=" + digest, len(content)])
    writer.writerow([metadata + "/RECORD", "", ""])
    return output.getvalue().encode()


__all__ = ["build_pack", "distribution_name"]
