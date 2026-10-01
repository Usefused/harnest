"""Terminate Studio-owned subprocess trees on supported release platforms."""

import os
import signal
import subprocess


def terminate_tree(pid: int, *, force: bool = False) -> None:
    """Include CLI descendants so stopping Studio cannot orphan a local agent server."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
