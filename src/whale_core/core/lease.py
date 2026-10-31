"""Single active Whale service per data directory on Linux/macOS.

Adapted from PR #4's RuntimeLease, without replacing PR #3's runtime.
This is an advisory process lock, not an authentication or sandbox boundary.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class RuntimeBusy(RuntimeError):
    """Another CLI/API/desktop host owns the same configuration directory."""


class RuntimeLease:
    """Keep the lock descriptor open for the entire service lifetime."""

    def __init__(self, directory: str | Path):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd: int | None = os.open(
            path / ".runtime.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600
        )
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise RuntimeBusy("another Whale runtime already uses this data directory") from exc

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> RuntimeLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
