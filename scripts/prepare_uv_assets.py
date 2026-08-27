#!/usr/bin/env python3
"""Download, verify, and stage pinned uv executables for Go cross-builds."""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIRECTORY = ROOT / "internal" / "uvbootstrap" / "assets" / "release"
VERSION_FILE = ROOT / "internal" / "uvbootstrap" / "version.txt"
RELEASE_BASE = "https://releases.astral.sh/github/uv/releases/download"
# Pin digests in source so a release cannot accept an archive and checksum that
# were both changed at the download origin.
TARGETS = {
    "darwin-amd64": (
        "uv-x86_64-apple-darwin.tar.gz",
        "uv_darwin_amd64",
        "2a26ea71bbeff1c7e12c2cc40245c96a041deff276bc921e7038e304d5d3e04c",
    ),
    "darwin-arm64": (
        "uv-aarch64-apple-darwin.tar.gz",
        "uv_darwin_arm64",
        "14b459d51ea2e71eeba28c45a268c922bdf8607fc6455e3f40b4e082895d160d",
    ),
    "linux-amd64": (
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "uv_linux_amd64",
        "8681d8921e7d520fb368991dcf5f9c1905b80f5bf2a265a0ed085c8d8e342477",
    ),
    "linux-arm64": (
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "uv_linux_arm64",
        "d58030acd26159499ac82f32da12d1b3c12a3a1bfc414232d9082070c03e128d",
    ),
    "windows-amd64": (
        "uv-x86_64-pc-windows-msvc.zip",
        "uv_windows_amd64.exe",
        "df7cb9f243eae1621400d4fcf5b1b3d90f20e264ece91b64deb3b0078abca6ef",
    ),
    "windows-arm64": (
        "uv-aarch64-pc-windows-msvc.zip",
        "uv_windows_arm64.exe",
        "6dda514fbbe3152d980758e0f6347116060114d7d24932fc0ea5d8063f8b253a",
    ),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "harnest-release"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def verify_archive(archive: bytes, expected: str, name: str) -> None:
    actual = hashlib.sha256(archive).hexdigest()
    if not expected or actual != expected:
        raise ValueError(f"checksum mismatch for {name}: expected {expected}, got {actual}")


def executable_from_tar(archive: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [item for item in bundle.getmembers() if item.isfile() and Path(item.name).name == "uv"]
        if len(members) != 1:
            raise ValueError(f"uv archive contains {len(members)} executable candidates")
        stream = bundle.extractfile(members[0])
        if stream is None:
            raise ValueError("uv executable cannot be read from archive")
        return stream.read()


def executable_from_zip(archive: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [name for name in bundle.namelist() if Path(name).name == "uv.exe"]
        if len(names) != 1:
            raise ValueError(f"uv archive contains {len(names)} executable candidates")
        return bundle.read(names[0])


def executable_from_archive(name: str, archive: bytes) -> bytes:
    if name.endswith(".zip"):
        return executable_from_zip(archive)
    return executable_from_tar(archive)


def prepare_target(version: str, target: str, output_directory: Path) -> None:
    archive_name, output_name, expected_checksum = TARGETS[target]
    archive_url = f"{RELEASE_BASE}/{version}/{archive_name}"
    archive = download(archive_url)
    verify_archive(archive, expected_checksum, archive_name)
    executable = executable_from_archive(archive_name, archive)
    (output_directory / output_name).write_bytes(executable)
    print(f"staged uv {version} for {target}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=VERSION_FILE.read_text(encoding="utf-8").strip())
    parser.add_argument("--target", action="append", choices=sorted(TARGETS))
    parser.add_argument("--output-directory", type=Path, default=ASSET_DIRECTORY)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    for target in arguments.target or sorted(TARGETS):
        prepare_target(arguments.version, target, arguments.output_directory)


if __name__ == "__main__":
    main()
