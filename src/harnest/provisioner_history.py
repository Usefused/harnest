"""Atomic deployment state and immutable revision snapshots with paginated history."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .provisioner_config import Deployment, ProvisionError
from .provisioner_lock import private_file


def timestamp() -> str:
    """Use UTC timestamps that sort consistently across Studio and CLI processes."""

    return datetime.now(timezone.utc).isoformat()


class RevisionStore:
    """Commit active revision and attempt outcome together; never archive resolved credentials."""

    def __init__(self, directory: Path):
        """Bind history to the already locked, private deployment state directory."""

        self.directory = directory

    @contextmanager
    def connection(self):
        """Keep database files private and reject links before opening the SQLite journal."""

        path = self.directory / "revisions.sqlite3"
        for suffix in ("", "-journal", "-wal", "-shm"):
            if Path(str(path) + suffix).is_symlink():
                raise ProvisionError("Provisioner history cannot use symbolic links")
        descriptor = private_file(path)
        os.close(descriptor)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            self._initialize(connection)
            yield connection
        except sqlite3.Error:
            raise ProvisionError("Cannot read or commit deployment history") from None
        finally:
            connection.close()

    def _initialize(self, connection) -> None:
        """Create one source of truth and import an earlier credential-free JSON journal once."""

        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ProvisionError("Deployment history requires a newer Harnest version")
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id = 1), document TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS revisions (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, completed_at TEXT,
                operation TEXT NOT NULL, status TEXT NOT NULL,
                rollback_of INTEGER, fingerprint TEXT NOT NULL,
                snapshot TEXT NOT NULL, summary TEXT NOT NULL, images TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS revisions_status ON revisions(status)")
            connection.execute("PRAGMA user_version = 1")
            if connection.execute("SELECT 1 FROM state WHERE id = 1").fetchone() is None:
                self._import_legacy(connection)

    def _import_legacy(self, connection) -> None:
        """Preserve pre-versioning ownership without inventing a rollbackable revision."""

        path = self.directory / "state.json"
        if path.is_symlink():
            raise ProvisionError("Provisioner state cannot use symbolic links")
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
        except (OSError, ValueError):
            raise ProvisionError("Cannot import the previous deployment journal") from None
        self._save(connection, state)

    def read(self) -> dict:
        """Read the singleton deployment state without loading any historical snapshots."""

        with self.connection() as connection:
            row = connection.execute("SELECT document FROM state WHERE id = 1").fetchone()
            return json.loads(row[0]) if row else {}

    def recover(self) -> None:
        """An applying record under a newly acquired process lock represents an interrupted attempt."""

        with self.connection() as connection, connection:
            connection.execute("UPDATE revisions SET status = 'interrupted', completed_at = ? WHERE status = 'applying'", (timestamp(),))
            row = connection.execute("SELECT document FROM state WHERE id = 1").fetchone()
            state = json.loads(row[0]) if row else {}
            if state.get("status") == "applying":
                self._save(connection, {**state, "status": "interrupted"})

    def begin(self, plan, state: dict, operation: str, images: dict, rollback_of: int | None) -> dict:
        """Allocate a monotonic revision and record recovery ownership before the first backend write."""

        snapshot = json.dumps(plan.deployment.model_dump(mode="json"), sort_keys=True)
        with self.connection() as connection, connection:
            row = connection.execute("""INSERT INTO revisions
                (created_at, operation, status, rollback_of, fingerprint, snapshot, summary, images)
                VALUES (?, ?, 'applying', ?, ?, ?, ?, ?)""", (
                    timestamp(), operation, rollback_of, hashlib.sha256(snapshot.encode()).hexdigest(),
                    snapshot, json.dumps(plan.summary()), json.dumps(images)))
            result = {**state, "attempt_revision": row.lastrowid, "operation": operation, "status": "applying"}
            self._save(connection, result)
        return result

    def write(self, state: dict) -> None:
        """Commit the active pointer and revision outcome in the same durable transaction."""

        with self.connection() as connection, connection:
            self._save(connection, state)
            if state.get("operation") not in {"apply", "rollback"}:
                return
            outcome = {"ready": "succeeded", "failed": "failed"}.get(state.get("status"))
            if outcome and state.get("attempt_revision"):
                connection.execute("UPDATE revisions SET status = ?, completed_at = ? WHERE revision = ?", (
                    outcome, timestamp(), state["attempt_revision"]))

    def _save(self, connection, state: dict) -> None:
        """Store current ownership once rather than duplicating it in the historical records."""

        connection.execute("INSERT OR REPLACE INTO state (id, document) VALUES (1, ?)", (json.dumps(state),))

    def history(self, limit: int = 20, before: int | None = None) -> dict:
        """Filter, order, project, and paginate in SQLite; never expose snapshot contents."""

        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ProvisionError("History requires a limit from 1 to 100 and a positive revision cursor")
        with self.connection() as connection:
            rows = connection.execute("""SELECT revision, created_at, completed_at, operation,
                status, rollback_of, fingerprint, summary, images FROM revisions
                WHERE revision < ? ORDER BY revision DESC LIMIT ?""", (before or 9_223_372_036_854_775_807, limit + 1)).fetchall()
        return {"revisions": [_metadata(row) for row in rows[:limit]],
                "next_before": rows[limit - 1]["revision"] if len(rows) > limit else None}

    def snapshot(self, revision: int, *, successful: bool = True) -> Deployment:
        """Validate a selected immutable snapshot and allow rollback only to a successful attempt."""

        with self.connection() as connection:
            row = connection.execute("SELECT status, snapshot, fingerprint FROM revisions WHERE revision = ?", (revision,)).fetchone()
        if row is None:
            raise ProvisionError("Deployment revision does not exist")
        if successful and row["status"] != "succeeded":
            raise ProvisionError("Rollback requires a successfully deployed revision")
        if hashlib.sha256(row["snapshot"].encode()).hexdigest() != row["fingerprint"]:
            raise ProvisionError("Deployment snapshot does not match its recorded fingerprint")
        return Deployment.model_validate_json(row["snapshot"])


def _metadata(row) -> dict:
    """Expose release identity and image references, excluding authored connection values."""

    value = dict(row)
    summary = json.loads(value.pop("summary"))
    value["release"] = summary.get("release")
    value["images"] = json.loads(value["images"])
    return value
