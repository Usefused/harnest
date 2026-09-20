"""Supervised, bounded subprocess jobs using argv rather than a browser-controlled shell."""

from __future__ import annotations

from collections import deque
import subprocess
from threading import RLock, Thread
import time
import uuid

from fastapi import HTTPException

from .processes import terminate_tree


class Jobs:
    """Track CLI output, serialize mutations, and terminate child process groups on shutdown."""

    def __init__(self, cli: str, workspace):
        """Bind the executable and workspace outside the HTTP command surface."""
        self.cli = cli
        self.workspace = workspace
        self.lock = RLock()
        self.items = {}
        self.processes = {}
        self.closed = False

    def start(self, args: list[str], project: str, *, serving: bool = False) -> dict:
        """Allow one finite command per workspace and a separate long-running preview."""
        with self.lock:
            self.ensure_idle(serving=serving)
            self._prune()
            job = {"id": uuid.uuid4().hex, "project": project, "argv": [self.cli, *args], "status": "running", "exit_code": None, "output": "", "serving": serving, "started": time.time()}
            self.items[job["id"]] = job
            Thread(target=self._run, args=(job,), daemon=True).start()
            return dict(job)

    def ensure_idle(self, *, serving: bool = False) -> None:
        """Reject overlapping compiler or filesystem mutations instead of racing a save."""
        if self.closed:
            raise HTTPException(503, "The builder is shutting down.")
        if any(j["status"] == "running" and j["serving"] == serving for j in self.items.values()):
            raise HTTPException(409, "Wait for or stop the running command first.")

    def _prune(self) -> None:
        """Bound retained history without dropping live process ownership."""
        completed = [key for key, value in self.items.items() if value["status"] != "running"]
        for key in completed[:-19]:
            self.items.pop(key)

    def list(self) -> list[dict]:
        """Return snapshots so output readers never observe half-mutated job state."""
        with self.lock:
            return [dict(j) for j in self.items.values()]

    def _run(self, job: dict) -> None:
        """Drain bounded output continuously so verbose commands cannot block on a full pipe."""
        try:
            self._execute(job)
        except OSError as error:
            with self.lock:
                job.update(status="failed", output=f"Unable to run Harnest: {error}", exit_code=-1)
        finally:
            with self.lock:
                self.processes.pop(job["id"], None)

    def _execute(self, job: dict) -> None:
        """Start a process group and enforce a finite-command timeout with descendant cleanup."""
        with self.lock:
            if job["status"] != "running":
                return
            process = subprocess.Popen(job["argv"], cwd=self.workspace.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.processes[job["id"]] = process
        reader = Thread(target=self._output, args=(job, process), daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=None if job["serving"] else 900)
        except subprocess.TimeoutExpired:
            self.stop(job["id"])
            code = process.wait()
            with self.lock:
                job["output"] += "\nCommand timed out after 15 minutes.\n"
        reader.join(timeout=2)
        with self.lock:
            if job["status"] == "running":
                job["status"] = "succeeded" if code == 0 else "failed"
            job["exit_code"] = code

    def _output(self, job: dict, process: subprocess.Popen) -> None:
        """Keep the last 128 KiB without waiting for newline-terminated progress output."""
        chunks = deque(maxlen=128)
        with process.stdout as stream:
            while data := stream.read1(1024):
                chunks.append(data)
                with self.lock:
                    job["output"] = b"".join(chunks).decode("utf-8", errors="replace")

    def stop(self, identity: str) -> dict:
        """Cancel queued starts too; terminate the entire group so reload children do not leak."""
        with self.lock:
            job = self.items.get(identity)
            if job is None:
                raise HTTPException(404, "Command not found.")
            if job["status"] != "running":
                return dict(job)
            job["status"] = "stopped"
            process = self.processes.get(identity)
        if process is not None:
            _terminate(process)
        return dict(job)

    def close(self) -> None:
        """Release all process ownership before the HTTP application exits."""
        with self.lock:
            self.closed = True
            identities = list(self.items)
        for identity in identities:
            self.stop(identity)


def _terminate(process: subprocess.Popen) -> None:
    """Escalate after a short graceful shutdown window, tolerating already-reaped children."""
    try:
        terminate_tree(process.pid)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        terminate_tree(process.pid, force=True)
        process.wait(timeout=3)
    except ProcessLookupError:
        pass
