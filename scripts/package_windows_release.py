"""Build MSI assets from checksum-verified Windows binaries in an existing release."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import zipfile

from build_windows_installer import build


def release_command(*args: str, repo: str) -> str:
    """Scope every GitHub operation to the workflow's selected repository."""
    return subprocess.check_output(["gh", "release", *args, "--repo", repo], text=True).strip()


def extract_cli(archive: Path, checksums: str, destination: Path) -> Path:
    """Authenticate the archive against its manifest before extracting the one executable."""
    entries = re.findall(r"(?m)^([0-9a-fA-F]{64})\s+\*?" + re.escape(archive.name) + r"$", checksums)
    if len(entries) != 1 or hashlib.sha256(archive.read_bytes()).hexdigest() != entries[0].lower():
        raise ValueError(f"Checksum verification failed for {archive.name}")
    destination.mkdir(parents=True, exist_ok=True)
    cli = destination / "harnest.exe"
    with zipfile.ZipFile(archive) as package:
        cli.write_bytes(package.read("harnest.exe"))
    return cli


def package_release(repo: str, tag: str, output: Path) -> list[Path]:
    """Retain archive checksum entries when adding MSI assets, including on a rerun."""
    if tag == "latest":
        tag = release_command("view", "--json", "tagName", "--jq", ".tagName", repo=repo)
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Windows MSI packaging requires a stable release tag")
    version = tag[1:]
    output.mkdir(parents=True, exist_ok=True)
    release_command("download", tag, "--pattern", "checksums.txt", "--pattern",
                    f"harnest_{version}_windows_*.zip", "--dir", str(output), "--clobber", repo=repo)
    manifest = output / "checksums.txt"
    checksums = manifest.read_text(encoding="utf-8")
    # Publish the architecture whose complete installation has passed native validation.
    archive = output / f"harnest_{version}_windows_amd64.zip"
    cli = extract_cli(archive, checksums, output / "amd64")
    artifacts = [build(cli, version, "amd64", output)]
    names = {artifact.name for artifact in artifacts}
    lines = [line for line in checksums.splitlines() if line.strip() and line.split()[-1].lstrip("*") not in names]
    for artifact in artifacts:
        lines.append(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [*artifacts, manifest]


def main() -> None:
    """Prepare packages for the workflow to test before release attachment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/windows-installer"))
    args = parser.parse_args()
    package_release(args.repo, args.tag, args.output)


if __name__ == "__main__":
    main()
