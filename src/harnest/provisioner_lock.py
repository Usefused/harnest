"""Private, nonblocking provisioner locks on POSIX and Windows hosts."""

from contextlib import contextmanager
import os

from .provisioner_config import ProvisionError


def private_file(path) -> int:
    """Reject linked state files and use the host's no-follow open flag where available."""
    if path.is_symlink():
        raise ProvisionError("Provisioner state cannot use symbolic links")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if path.is_symlink() or os.fstat(descriptor).st_ino != path.stat().st_ino:
            raise ProvisionError("Provisioner state changed while opening it")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def exclusive_lock(path):
    """Hold ownership until the descriptor closes, including after process interruption."""
    with os.fdopen(private_file(path), "r+b") as stream:
        try:
            _lock(stream)
        except OSError:
            raise ProvisionError("Another provisioner operation is running") from None
        yield


def _lock(stream) -> None:
    """Use native locks so Studio can import and launch on every released platform."""
    if os.name == "nt":
        import msvcrt
        # Windows byte-range locks require a byte even for an initially empty file.
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
