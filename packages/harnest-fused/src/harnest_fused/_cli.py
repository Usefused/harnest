"""Bounded subprocess calls without parsing human output or retaining secrets."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess
from typing import Any


class FusedCLIError(RuntimeError):
    """A failed provisioning phase; previous remote changes are not rolled back."""


@dataclass(frozen=True)
class CLI:
    """Use the installed CLI's configured authority with no shell or retries."""

    executable: str
    directory: Path
    timeout_seconds: float

    @classmethod
    def create(cls, executable: str, directory: Path, timeout_seconds: float) -> CLI:
        """Check local prerequisites before creating plans or remote state."""

        resolved = shutil.which(executable)
        if resolved is None:
            raise FusedCLIError("fused-cli is required for setup; install it and configure an Engine first")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("setup timeout_seconds must be finite and positive")
        return cls(resolved, directory, timeout_seconds)

    def run(self, phase: str, arguments: list[str], *, structured: bool = True) -> Any:
        """Capture JSON only; leave human MCP apply and its token handoff visible."""

        command = [self.executable, "--no-input", *arguments]
        if structured:
            command.append("--json")
        failure = None
        try:
            result = subprocess.run(
                command, cwd=self.directory, stdin=subprocess.DEVNULL,
                capture_output=structured, text=True, check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            # Exception objects may retain credentials in child stdout/stderr.
            # Raise outside the handler so even __context__ stays credential-free.
            failure = FusedCLIError(f"{phase} failed with {type(error).__name__}; inspect saved receipts before retrying")
        if failure is not None:
            raise failure
        if result.returncode:
            raise FusedCLIError(f"{phase} failed (exit {result.returncode}); earlier changes may have committed; inspect saved receipts")
        return _decode(result.stdout, phase) if structured else None


def _decode(output: str, phase: str) -> Any:
    """Require one JSON value without exposing malformed provider output."""

    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        value = None
    if not isinstance(value, (dict, list)):
        raise FusedCLIError(f"{phase} returned invalid JSON; inspect saved receipts before retrying")
    return value


def required_string(value: dict[str, Any], key: str, phase: str) -> str:
    """Reject incomplete machine-readable identities before the next phase."""

    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise FusedCLIError(f"{phase} omitted {key}; inspect saved receipts")
    return result
