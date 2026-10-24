"""POSIX advisory lock preventing two local processes from trading one account."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ProcessLockAlreadyHeld(RuntimeError):
    """Another process owns the configured system lock file."""


class ProcessFileLock:
    """Hold a non-blocking OS file lock for the lifetime of the application."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("this ProcessFileLock instance already owns its lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProcessLockAlreadyHeld(
                    f"another process holds the trading lock at {self._path}"
                ) from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> ProcessFileLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
