"""Run explicitly classified Harnest Python test tiers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


TIERS = ("unit", "integration", "e2e", "live")
_ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT = _ROOT / "tests" / "python"
_MANIFEST = _TEST_ROOT / "suites.json"


class TestSuiteManifestError(ValueError):
    """Report an invalid or incomplete test-suite classification."""


def load_manifest(path: Path = _MANIFEST) -> dict[str, object]:
    """Load the checked-in suite manifest without importing test modules."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TestSuiteManifestError("test suite manifest must be a JSON object")
    return value


def validate_manifest(
    manifest: dict[str, object], test_root: Path = _TEST_ROOT
) -> dict[str, str]:
    """Require every test module to have one valid default tier."""

    declared = manifest.get("modules")
    if not isinstance(declared, dict) or set(declared) != set(TIERS):
        raise TestSuiteManifestError(
            f"manifest modules must define exactly: {', '.join(TIERS)}"
        )
    assignments = _module_assignments(declared)
    _validate_module_inventory(assignments, test_root)
    _validated_overrides(manifest)
    return assignments


def _module_assignments(declared: dict[str, object]) -> dict[str, str]:
    """Normalize tier lists while rejecting duplicate module ownership."""

    assignments: dict[str, str] = {}
    for tier in TIERS:
        modules = declared[tier]
        if not isinstance(modules, list) or not all(
            isinstance(module, str) for module in modules
        ):
            raise TestSuiteManifestError(f"manifest tier {tier!r} must be a string list")
        for module in modules:
            if module in assignments:
                raise TestSuiteManifestError(
                    f"test module {module!r} belongs to multiple tiers"
                )
            assignments[module] = tier
    return assignments


def _validate_module_inventory(
    assignments: dict[str, str], test_root: Path
) -> None:
    """Keep the manifest and discoverable test files in exact agreement."""

    available = _available_modules(test_root)
    missing = sorted(available - assignments.keys())
    stale = sorted(assignments.keys() - available)
    if missing or stale:
        details = []
        if missing:
            details.append(f"unclassified: {', '.join(missing)}")
        if stale:
            details.append(f"missing files: {', '.join(stale)}")
        raise TestSuiteManifestError("; ".join(details))


def _available_modules(test_root: Path) -> set[str]:
    """Include repository tests that Git tracks or would allow to be added."""

    if test_root.resolve() != _TEST_ROOT.resolve():
        return {path.stem for path in test_root.glob("test_*.py")}
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "tests/python/test_*.py",
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Source archives have no Git metadata and cannot contain ignored local files.
        return {path.stem for path in test_root.glob("test_*.py")}
    # The index still lists files deleted or renamed in the working tree; only
    # executable files should participate in the suite contract before commit.
    return {
        Path(line).stem
        for line in result.stdout.splitlines()
        if line and (_ROOT / line).is_file()
    }


def _validated_overrides(manifest: dict[str, object]) -> dict[str, tuple[str, ...]]:
    """Normalize narrower test-ID overrides and reject ambiguous prefixes."""

    configured = manifest.get("overrides", {})
    if not isinstance(configured, dict) or not set(configured).issubset(TIERS):
        raise TestSuiteManifestError("manifest overrides use an unknown tier")
    overrides: dict[str, tuple[str, ...]] = {}
    owners: dict[str, str] = {}
    for tier, prefixes in configured.items():
        if not isinstance(prefixes, list) or not all(
            isinstance(prefix, str) for prefix in prefixes
        ):
            raise TestSuiteManifestError(
                f"manifest overrides for {tier!r} must be a string list"
            )
        for prefix in prefixes:
            if prefix in owners:
                raise TestSuiteManifestError(
                    f"test override {prefix!r} belongs to multiple tiers"
                )
            owners[prefix] = tier
        overrides[tier] = tuple(prefixes)
    return overrides


def _tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Flatten discovery output while preserving its deterministic order."""

    values: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            values.extend(_tests(item))
        else:
            values.append(item)
    return values


def _override_for(
    test_id: str, overrides: dict[str, tuple[str, ...]]
) -> tuple[str, str] | None:
    """Resolve at most one most-specific tier override for a concrete test."""

    matches = [
        (tier, prefix)
        for tier, prefixes in overrides.items()
        for prefix in prefixes
        if test_id == prefix or test_id.startswith(prefix + ".")
    ]
    if len(matches) > 1:
        raise TestSuiteManifestError(
            f"discovered test {test_id!r} matches multiple overrides"
        )
    return matches[0] if matches else None


def _validate_matched_overrides(
    overrides: dict[str, tuple[str, ...]], matched: set[str]
) -> None:
    """Reject renamed or removed test IDs retained by the manifest."""

    configured = {prefix for prefixes in overrides.values() for prefix in prefixes}
    stale = sorted(configured - matched)
    if stale:
        raise TestSuiteManifestError(
            f"test overrides matched no tests: {', '.join(stale)}"
        )


def _selected_modules(
    assignments: dict[str, str],
    overrides: dict[str, tuple[str, ...]],
    selected: tuple[str, ...],
) -> list[str]:
    """Load default-tier modules plus modules containing selected overrides."""

    modules = {
        module for module, tier in assignments.items() if tier in selected
    }
    modules.update(
        prefix.split(".", 1)[0]
        for tier in selected
        for prefix in overrides.get(tier, ())
    )
    return sorted(modules)


def classified_tests(
    manifest: dict[str, object],
    test_root: Path = _TEST_ROOT,
    selected: tuple[str, ...] = TIERS,
) -> dict[str, list[unittest.TestCase]]:
    """Discover once and assign every concrete test to exactly one tier."""

    assignments = validate_manifest(manifest, test_root)
    overrides = _validated_overrides(manifest)
    discovered = unittest.defaultTestLoader.loadTestsFromNames(
        _selected_modules(assignments, overrides, selected)
    )
    classified: dict[str, list[unittest.TestCase]] = {tier: [] for tier in TIERS}
    matched_overrides: set[str] = set()
    for test in _tests(discovered):
        test_id = test.id()
        module = test_id.split(".", 1)[0]
        tier = assignments.get(module)
        if tier is None:
            raise TestSuiteManifestError(
                f"discovered test {test_id!r} has no classified module"
            )
        override = _override_for(test_id, overrides)
        if override:
            tier, prefix = override
            matched_overrides.add(prefix)
        if tier in selected:
            classified[tier].append(test)

    selected_overrides = {
        tier: prefixes for tier, prefixes in overrides.items() if tier in selected
    }
    _validate_matched_overrides(selected_overrides, matched_overrides)
    return classified


def _parser() -> argparse.ArgumentParser:
    """Build the small command surface used by Make and CI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tiers",
        nargs="*",
        metavar="TIER",
        help="test tiers to run (default: all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print tier counts without running tests",
    )
    return parser


def _install_import_paths() -> None:
    """Keep this checkout authoritative in this runner and child processes."""

    paths = [str(_TEST_ROOT), str(_ROOT / "src"), str(_ROOT)]
    sys.path[:0] = paths
    inherited = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [*paths, *([inherited] if inherited else [])]
    )


def main(argv: list[str] | None = None) -> int:
    """Run selected tiers once or report their concrete test counts."""

    arguments = _parser().parse_args(argv)
    _install_import_paths()
    try:
        requested = arguments.tiers or ["all"]
        unknown = sorted(set(requested) - {*TIERS, "all"})
        if unknown:
            raise TestSuiteManifestError(
                f"unknown test tiers: {', '.join(unknown)}"
            )
        selected = TIERS if "all" in requested else tuple(dict.fromkeys(requested))
        classified = classified_tests(load_manifest(), selected=selected)
    except (OSError, json.JSONDecodeError, TestSuiteManifestError) as error:
        print(f"invalid Python test suite manifest: {error}", file=sys.stderr)
        return 2
    if arguments.list:
        for tier in selected:
            print(f"{tier}: {len(classified[tier])}")
        return 0
    suite = unittest.TestSuite(
        test for tier in selected for test in classified[tier]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
