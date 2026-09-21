"""Project-scoped execution credentials, outside source trees and model context."""

import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock

from .files import replace


class CredentialStore:
    """Keep runtime bindings in owner-only local files, addressed by resolved project identity."""

    def __init__(self, directory=None):
        """Allow a private directory override for packaged launchers and isolated tests."""
        self.directory = Path(directory or os.getenv("HARNEST_BUILDER_CREDENTIALS_DIR") or Path.home() / ".harnest" / "studio-credentials").resolve()
        self.lock = RLock()

    def _path(self, project):
        """Reject linked storage paths before reading or replacing credential files."""
        if self.directory.is_relative_to(project.resolve()):
            raise OSError("Credential storage must be outside the agent project")
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise OSError("Credential storage cannot use symbolic links")
        digest = hashlib.sha256(str(project.resolve()).encode()).hexdigest()
        path = self.directory / (digest + ".json")
        if path.is_symlink():
            raise OSError("Credential file cannot be a symbolic link")
        return path

    def read(self, project):
        """Read only bounded environment bindings; callers must never serialize them to models."""
        with self.lock:
            path = self._path(project)
            if not path.exists():
                return {}
            if path.stat().st_size > 65536 or path.stat().st_mode & 0o077:
                raise OSError("Credential file must be private and bounded")
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or any(not re.fullmatch(r"HARNEST_MCP_[A-Z0-9_]+", key) or not isinstance(value, str) for key, value in data.items()):
                raise OSError("Invalid credential bindings")
            return data

    def save(self, project, bindings):
        """Atomically merge approved bindings without exposing plaintext to project files."""
        with self.lock:
            data = {**self.read(project), **bindings}
            if len(json.dumps(data).encode()) > 65536:
                raise OSError("Too many credential bindings")
            path = self._path(project)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            replace(path, json.dumps(data))
            path.chmod(0o600)
