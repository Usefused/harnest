"""Exercise MSI install, repair, upgrade, rollback, and uninstall on a disposable Windows runner."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import winreg

from build_windows_installer import build

POLICY_KEY = r"Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell"


def user_path() -> str:
    """Read persisted PATH rather than the launching process's stale environment."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        return str(winreg.QueryValueEx(key, "Path")[0])


def installed_path() -> str:
    """Use the MSI-authored key to locate the installed executable."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Fused\Harnest") as key:
        return str(winreg.QueryValueEx(key, "InstallDir")[0])


def run_msi(package: Path, operation: str, log: Path, *, success: bool = True) -> None:
    """Wait for Windows Installer and retain a verbose diagnostic log on failure."""
    result = subprocess.run(["msiexec.exe", operation, str(package.resolve()), "/qn", "/norestart",
                             "/l*v", str(log.resolve())], timeout=900, check=False)
    passed = result.returncode in (0, 3010)
    if passed != success:
        raise RuntimeError(f"Unexpected MSI exit {result.returncode}; see {log}")


def verify_cli(directory: str) -> None:
    """Verify a real installed executable and runtime with ordinary CLI commands."""
    cli = str(Path(directory) / "harnest.exe")
    subprocess.run([cli, "--version"], check=True, timeout=30)
    subprocess.run([cli, "doctor"], check=True, timeout=120)
    if directory.rstrip("\\").lower() not in {item.rstrip("\\").lower() for item in user_path().split(";")}:
        raise AssertionError("Installer did not add the CLI directory to user PATH")


def exercise(package: Path, cli: Path, output: Path, arch: str) -> None:
    """Check native bootstrap failure before exercising install and upgrade with release bytes."""
    original_path = user_path()
    with tempfile.TemporaryDirectory(prefix="harnest-msi-failure-") as temporary:
        fixture = Path(temporary)
        # An actual failing PE exercises MSI's rollback handling without mocking msiexec.
        (fixture / "main.go").write_text('package main\nimport "os"\nfunc main() { os.Exit(42) }\n')
        bad_cli = fixture / "harnest.exe"
        subprocess.run(["go", "build", "-o", str(bad_cli), str(fixture / "main.go")], check=True)
        failure = build(bad_cli, "254.0.0", arch, output / "failure")
        run_msi(failure, "/i", output / "failure.log", success=False)
        if user_path() != original_path:
            raise AssertionError("Failed installation changed PATH")
    run_msi(package, "/i", output / "install.log")
    try:
        directory = installed_path()
        verify_cli(directory)
        run_msi(failure, "/i", output / "failed-upgrade.log", success=False)
        verify_cli(directory)
        run_msi(package, "/fa", output / "repair.log")
        verify_cli(directory)
        upgrade = build(cli, "254.0.1", arch, output / "upgrade")
        run_msi(upgrade, "/i", output / "upgrade.log")
        package = upgrade
        verify_cli(directory)
        run_msi(package, "/x", output / "uninstall.log")
        if Path(directory, "harnest.exe").exists() or user_path() != original_path:
            raise AssertionError("Uninstall did not restore CLI/PATH ownership")
    finally:
        # Cleanup retains the original assertion and its diagnostic logs.
        subprocess.run(["msiexec.exe", "/x", str(package.resolve()), "/qn", "/norestart"],
                       check=False, timeout=120)


def main() -> None:
    """Prove installation works while script execution is restricted, restoring the runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output", type=Path, default=Path(".cache/windows-installer-smoke"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # Hosted runners may pass a process override to children; test the actual restricted policy.
    os.environ.pop("PSExecutionPolicyPreference", None)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, POLICY_KEY) as key:
        try:
            previous = winreg.QueryValueEx(key, "ExecutionPolicy")
        except FileNotFoundError:
            previous = None
        winreg.SetValueEx(key, "ExecutionPolicy", 0, winreg.REG_SZ, "Restricted")
        try:
            policy = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command",
                                              "Get-ExecutionPolicy"], text=True).strip()
            if policy != "Restricted":
                raise AssertionError(f"Expected effective Restricted policy, found {policy}")
            exercise(args.package, args.cli, args.output, args.arch)
            if winreg.QueryValueEx(key, "ExecutionPolicy")[0] != "Restricted":
                raise AssertionError("MSI changed PowerShell execution policy")
        finally:
            if previous is None:
                winreg.DeleteValue(key, "ExecutionPolicy")
            else:
                winreg.SetValueEx(key, "ExecutionPolicy", 0, previous[1], previous[0])


if __name__ == "__main__":
    main()
