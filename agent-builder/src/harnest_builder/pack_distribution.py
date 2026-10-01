"""Build a company launcher wheel containing validated, immutable Studio Packs."""

import base64
import csv
from io import StringIO
import hashlib
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

import yaml
from packaging.version import Version

from .pack_manifest import STUDIO_VERSION
from .packs import Packs


def package_packs(roots, output: Path, distribution: str, version: str) -> Path:
    """Produce a portable wheel with pack assets, one launcher, and exact Studio dependency."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", distribution):
        raise ValueError("Distribution names must be lowercase words separated by hyphens")
    version = str(Version(version))
    catalog = Packs(roots)
    module = distribution.replace("-", "_") + "_studio"
    contents = {f"{module}/__init__.py": b""}
    embedded = []
    for identity, pack in catalog.packs.items():
        embedded.append(identity)
        _embed(contents, module, identity, pack)
    source = ('"""Launch the company Studio distribution with its bundled packs."""\n'
              'from pathlib import Path\nfrom harnest_builder.__main__ import main as studio\n\n'
              'def main():\n    """Keep packaged resources independent of the caller working directory."""\n'
              f'    root = Path(__file__).parent / "packs"\n    studio(embedded_packs=[root / name for name in {embedded!r}])\n')
    contents[f"{module}/launcher.py"] = source.encode()
    normalized = distribution.replace("-", "_")
    metadata = f"{normalized}-{version}.dist-info"
    contents[f"{metadata}/METADATA"] = (f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\nSummary: Company Harnest Studio distribution\nRequires-Python: >=3.11\nRequires-Dist: harnest-agent-builder=={STUDIO_VERSION}\n").encode()
    contents[f"{metadata}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: harnest-studio\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    contents[f"{metadata}/entry_points.txt"] = f"[console_scripts]\n{distribution} = {module}.launcher:main\n".encode()
    contents[f"{metadata}/RECORD"] = _record(contents, metadata)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{normalized}-{version}-py3-none-any.whl"
    with target.open("xb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for path, content in sorted(contents.items()):
            archive.writestr(path, content)
    return target


def _embed(contents, module, identity, pack):
    """Materialize snapshot contents rather than rereading files that may have changed after validation."""
    manifest = dict(pack["manifest"])
    resources = []
    for item in manifest["resources"]:
        resource = dict(item)
        resource["files"] = {}
        for index, (destination, text) in enumerate(pack["resources"][item["id"]]["files"].items()):
            relative = f"sources/{item['id']}/{index}{Path(destination).suffix}"
            resource["files"][destination] = relative
            contents[f"{module}/packs/{identity}/{relative}"] = text.encode()
        resources.append(resource)
    manifest["resources"] = resources
    contents[f"{module}/packs/{identity}/studio-pack.yaml"] = yaml.safe_dump(manifest, sort_keys=False).encode()


def _record(contents, metadata):
    """Write wheel RECORD hashes using the standard URL-safe SHA-256 representation."""
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for path, content in sorted(contents.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([path, "sha256=" + digest, len(content)])
    writer.writerow([metadata + "/RECORD", "", ""])
    return output.getvalue().encode()
