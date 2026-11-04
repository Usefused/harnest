"""Package a release CLI as a per-user MSI using the pinned WiX build tool."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(cli: Path, version: str, arch: str, output: Path) -> Path:
    """Keep MSI versions representable and embed only the selected release executable."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("MSI packaging requires a stable major.minor.patch release")
    major, minor, patch = map(int, version.split("."))
    if major > 255 or minor > 255 or patch > 65535:
        raise ValueError("Version exceeds Windows Installer version limits")
    if arch not in ("amd64", "arm64"):
        raise ValueError("Unsupported Windows architecture")
    output.mkdir(parents=True, exist_ok=True)
    # Generate the UI license from the repository license to avoid maintaining two copies.
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    escaped = license_text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    license_rtf = output / "license.rtf"
    license_rtf.write_text("{\\rtf1\\ansi\n" + escaped.replace("\n", "\\par\n") + "}", encoding="utf-8")
    artifact = output / f"harnest_{version}_windows_{arch}.msi"
    subprocess.run([
        "wix", "build", str(ROOT / "packaging/windows/harnest.wxs"),
        "-arch", "x64" if arch == "amd64" else "arm64",
        "-ext", "WixToolset.UI.wixext", "-d", f"Version={version}",
        "-d", f"Cli={cli.resolve(strict=True)}", "-d", f"LicenseRtf={license_rtf.resolve()}",
        "-out", str(artifact.resolve()),
    ], check=True)
    return artifact


def main() -> None:
    """Expose the same packaging entry point to CI and release automation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/windows-installer"))
    args = parser.parse_args()
    print(build(args.cli, args.version, args.arch, args.output))


if __name__ == "__main__":
    main()
