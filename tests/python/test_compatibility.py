import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harnest.bundle import BundleImportError, compile_application, compile_artifact
from harnest.compatibility import (
    FRAMEWORK_COMPATIBILITY,
    ResolvedFrameworkCompatibility,
    validate_framework_compatibility,
)
from _session_store_fixture import write_session_store


class FrameworkCompatibilityTests(unittest.TestCase):
    def test_release_matrix_matches_framework_dependency_contract(self):
        self.assertEqual(
            {
                name: (item.distribution, item.versions)
                for name, item in FRAMEWORK_COMPATIBILITY.items()
            },
            {
                "adk": ("google-adk", ">=2.8,<3"),
                "langgraph": ("langgraph", ">=1.2,<2"),
            },
        )

    def test_ci_framework_constraints_stay_inside_the_release_contract(self):
        """Pinned and minimum lanes must track the advertised distribution ranges."""
        from packaging.specifiers import SpecifierSet

        root = Path(__file__).resolve().parents[2]
        for name in ("frameworks.txt", "frameworks-minimum.txt"):
            lines = (root / "requirements" / name).read_text().splitlines()
            versions = dict(line.split("==", 1) for line in lines if line and not line.startswith("#"))
            for contract in FRAMEWORK_COMPATIBILITY.values():
                with self.subTest(lane=name, framework=contract.framework):
                    self.assertIn(versions[contract.distribution], SpecifierSet(contract.versions))

    def test_supported_distribution_is_resolved_with_harnest_version(self):
        versions = {"harnest": "0.1.0", "langgraph": "1.9.4"}
        with patch(
            "harnest.compatibility.metadata.version",
            side_effect=lambda name: versions[name],
        ):
            resolved = validate_framework_compatibility("langgraph")

        self.assertEqual(
            resolved,
            ResolvedFrameworkCompatibility(
                harnest_version="0.1.0",
                framework="langgraph",
                distribution="langgraph",
                distribution_version="1.9.4",
                supported_versions=">=1.2,<2",
            ),
        )

    def test_missing_distribution_has_actionable_install_error(self):
        def version(name):
            if name == "harnest":
                return "0.1.0"
            raise metadata.PackageNotFoundError(name)

        with patch("harnest.compatibility.metadata.version", side_effect=version):
            with self.assertRaisesRegex(
                BundleImportError,
                r"Harnest 0\.1\.0 requires google-adk >=2\.8,<3.*"
                r"pip install 'harnest\[adk\]==0\.1\.0'",
            ):
                compile_application(
                    "/path/need/not/exist",
                    entrypoint="agent:root_agent",
                    framework="adk",
                )

    def test_managed_and_advanced_compilation_reject_unsupported_version(self):
        versions = {"harnest": "0.1.0", "langgraph": "2.0.0"}
        for mode in ("managed", "advanced"):
            with self.subTest(mode=mode), patch(
                "harnest.compatibility.metadata.version",
                side_effect=lambda name: versions[name],
            ):
                with self.assertRaisesRegex(
                    BundleImportError,
                    r"Harnest 0\.1\.0 supports langgraph >=1\.2,<2, but 2\.0\.0 "
                    r"is installed\. Upgrade Harnest",
                ):
                    compile_application(
                        "/path/need/not/exist",
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                        mode=mode,
                    )

    def test_artifact_manifest_records_validated_distribution_versions(self):
        resolved = ResolvedFrameworkCompatibility(
            harnest_version="0.1.0",
            framework="langgraph",
            distribution="langgraph",
            distribution_version="1.7.3",
            supported_versions=">=1.2,<2",
        )
        target = SimpleNamespace(name="root")
        backend = SimpleNamespace(
            lower_managed=Mock(return_value=target),
            wrap_managed=Mock(return_value=None),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            output = Path(directory) / "compiled"
            root.mkdir()
            (root / "agent.py").write_text(
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='test/model', "
                "instruction='Answer.')\n",
                encoding="utf-8",
            )
            (root / "instructions.md").write_text("Answer.\n", encoding="utf-8")
            write_session_store(root)
            with patch(
                "harnest.bundle.validate_framework_compatibility",
                return_value=resolved,
            ), patch("harnest.bundle.get_backend", return_value=backend):
                manifest = compile_artifact(root, output, framework="langgraph")

        self.assertEqual(manifest["harnestVersion"], "0.1.0")
        self.assertEqual(
            manifest["framework"],
            {
                "name": "langgraph",
                "mode": "managed",
                "distribution": "langgraph",
                "version": "1.7.3",
            },
        )


if __name__ == "__main__":
    unittest.main()
