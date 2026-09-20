"""Release-platform contracts for Studio-owned locks and subprocess cleanup."""

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-builder/src"))
from harnest_builder.processes import terminate_tree
from harnest.provisioner_config import ProvisionError
from harnest.provisioner_lock import _lock, exclusive_lock, private_file


class StudioPlatformTests(unittest.TestCase):
    """Keep platform-specific primitives behind tested ownership boundaries."""

    def test_lock_excludes_parallel_operations_and_releases_on_exception(self):
        """Actual descriptor locks survive failed operations without stale sentinel state."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with self.assertRaisesRegex(ValueError, "operation failed"):
                with exclusive_lock(path):
                    with self.assertRaisesRegex(ProvisionError, "Another provisioner"):
                        with exclusive_lock(path):
                            self.fail("overlapping owner acquired the lock")
                    raise ValueError("operation failed")
            with exclusive_lock(path):
                pass

    def test_windows_lock_owns_one_byte_from_the_start(self):
        """The byte-range lock needs a nonempty file and deterministic offset."""
        native = SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with tempfile.TemporaryFile() as stream, patch.dict(sys.modules, {"msvcrt": native}):
            with patch("harnest.provisioner_lock.os.name", "nt"):
                _lock(stream)
            self.assertEqual(os.fstat(stream.fileno()).st_size, 1)
            self.assertEqual(stream.tell(), 0)
            native.locking.assert_called_once_with(stream.fileno(), 2, 1)

    def test_windows_cleanup_targets_the_owned_process_tree(self):
        """Cancellation includes native CLI descendants, without invoking a shell."""
        with patch("harnest_builder.processes.subprocess.run") as run:
            with patch("harnest_builder.processes.os.name", "nt"):
                terminate_tree(2345)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "2345", "/T", "/F"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_private_open_closes_descriptor_if_the_file_changes(self):
        """A state-file replacement never leaves a descriptor open after validation fails."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            with patch.object(Path, "is_symlink", return_value=False), patch.object(Path, "stat", side_effect=FileNotFoundError), patch("harnest.provisioner_lock.os.close", wraps=os.close) as close:
                with self.assertRaises(FileNotFoundError):
                    private_file(path)
                close.assert_called_once()
