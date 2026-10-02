"""Distribute the browser contract with the Python host that implements it."""

from io import BytesIO
import json
from pathlib import Path
import tarfile


def package_types(output: Path) -> Path:
    """Export an npm-installable type archive without network access or a Node runtime."""
    source = Path(__file__).parent / "ui_sdk"
    version = json.loads((source / "package.json").read_text())["version"]
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"harnest-studio-ui-{version}.tgz"
    # Exclusive creation protects archives already pinned by an author's lockfile.
    with target.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in ("package.json", "index.d.ts"):
            content = (source / name).read_bytes()
            info = tarfile.TarInfo("package/" + name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, BytesIO(content))
    return target
