import hashlib
import importlib.util
import io
import tarfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prepare_uv_assets", ROOT / "scripts" / "prepare_uv_assets.py"
)
assert SPEC is not None and SPEC.loader is not None
PREPARE_UV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE_UV)


class UVAssetPreparationTests(unittest.TestCase):
    def test_extracts_only_uv_from_supported_archive_formats(self):
        executable = b"native uv executable"

        self.assertEqual(
            PREPARE_UV.executable_from_tar(make_tar(executable)), executable
        )
        self.assertEqual(
            PREPARE_UV.executable_from_zip(make_zip(executable)), executable
        )

    def test_checksum_verification_rejects_changed_archive(self):
        archive = b"verified archive"
        checksum = hashlib.sha256(archive).hexdigest()

        PREPARE_UV.verify_archive(archive, checksum, "uv.tar.gz")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            PREPARE_UV.verify_archive(archive + b"changed", checksum, "uv.tar.gz")

    def test_targets_match_supported_goreleaser_matrix(self):
        self.assertEqual(
            set(PREPARE_UV.TARGETS),
            {
                "darwin-amd64",
                "darwin-arm64",
                "linux-amd64",
                "linux-arm64",
                "windows-amd64",
                "windows-arm64",
            },
        )


def make_tar(contents: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("uv-target/uv")
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))
    return output.getvalue()


def make_zip(contents: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        archive.writestr("uv-target/uv.exe", contents)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
