"""Framework compatibility contract for this Harnest release.

Framework distribution ranges come from ``harnest/frameworks/frameworks.json``,
the single source of truth shared with the Go CLI.
Compilation validates the installed distribution before importing authored agent
code, so managed and advanced agents share the same compatibility boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from types import MappingProxyType
from typing import Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .frameworks.registry import framework_metadata


_SOURCE_VERSION = "1.8.0"  # x-release-please-version


@dataclass(frozen=True, slots=True)
class FrameworkCompatibility:
    """Supported distribution range for one framework backend."""

    framework: str
    distribution: str
    versions: str
    install_extra: str


@dataclass(frozen=True, slots=True)
class ResolvedFrameworkCompatibility:
    """Versions resolved and validated for one compilation."""

    harnest_version: str
    framework: str
    distribution: str
    distribution_version: str
    supported_versions: str


class FrameworkCompatibilityError(RuntimeError):
    """An installed framework is absent or unsupported by this release."""


# Each Harnest release updates frameworks.json together with its optional
# dependency ranges. Do not infer compatibility from whichever packages import.
FRAMEWORK_COMPATIBILITY: Mapping[str, FrameworkCompatibility] = MappingProxyType(
    {
        name: FrameworkCompatibility(
            framework=name,
            distribution=item.distribution,
            versions=item.versions,
            install_extra=item.install_extra,
        )
        for name, item in framework_metadata().items()
    }
)


def current_harnest_version() -> str:
    """Return the installed Harnest distribution version.

    The fallback keeps source-tree execution useful before an editable install;
    release automation must keep it aligned with ``pyproject.toml``.
    """

    try:
        return metadata.version("harnest")
    except metadata.PackageNotFoundError:
        return _SOURCE_VERSION


def validate_framework_compatibility(
    framework: str,
) -> ResolvedFrameworkCompatibility:
    """Resolve and validate the framework distribution installed at compile time."""

    try:
        contract = FRAMEWORK_COMPATIBILITY[framework]
    except KeyError as exc:
        supported = " or ".join(FRAMEWORK_COMPATIBILITY)
        raise FrameworkCompatibilityError(
            f"unknown framework {framework!r}; Harnest supports {supported}"
        ) from exc

    harnest_version = current_harnest_version()
    try:
        installed = metadata.version(contract.distribution)
    except metadata.PackageNotFoundError as exc:
        raise FrameworkCompatibilityError(
            f"Harnest {harnest_version} requires {contract.distribution} "
            f"{contract.versions} for framework {framework!r}, but the distribution "
            f"is not installed. Install a compatible framework with "
            f"`pip install 'harnest[{contract.install_extra}]=={harnest_version}'`."
        ) from exc

    try:
        parsed_version = Version(installed)
        supported_versions = SpecifierSet(contract.versions)
    except (InvalidVersion, InvalidSpecifier) as exc:
        raise FrameworkCompatibilityError(
            f"cannot validate installed {contract.distribution} version {installed!r} "
            f"against the Harnest {harnest_version} compatibility range "
            f"{contract.versions}"
        ) from exc

    if parsed_version not in supported_versions:
        raise FrameworkCompatibilityError(
            f"Harnest {harnest_version} supports {contract.distribution} "
            f"{contract.versions}, but {installed} is installed. Upgrade Harnest to "
            f"a release that supports {contract.distribution} {installed}, or install "
            f"a compatible framework version with `pip install "
            f"'{contract.distribution}{contract.versions}'`."
        )

    return ResolvedFrameworkCompatibility(
        harnest_version=harnest_version,
        framework=framework,
        distribution=contract.distribution,
        distribution_version=installed,
        supported_versions=contract.versions,
    )


__all__ = [
    "FRAMEWORK_COMPATIBILITY",
    "FrameworkCompatibility",
    "FrameworkCompatibilityError",
    "ResolvedFrameworkCompatibility",
    "current_harnest_version",
    "validate_framework_compatibility",
]
