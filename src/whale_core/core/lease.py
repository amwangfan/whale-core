"""Single active Whale service per data directory on Windows/Linux/macOS.

Adapted from PR #4's RuntimeLease, without replacing PR #3's runtime.
This is an advisory process lock, not an authentication or sandbox boundary.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path


class RuntimeBusy(RuntimeError):
    """Another CLI/API/desktop host owns the same configuration directory."""


def lock_descriptor(fd: int) -> None:
    """Take a nonblocking lock on an already-open file; the owner retains the descriptor."""
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise RuntimeBusy("another Whale runtime already uses this data directory") from exc
        raise


def unlock_descriptor(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class RuntimeLease:
    """Keep the lock descriptor open for the entire service lifetime."""

    def __init__(self, directory: str | Path):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd: int | None = os.open(
            path / ".runtime.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600
        )
        try:
            lock_descriptor(self._fd)
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            unlock_descriptor(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> RuntimeLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
