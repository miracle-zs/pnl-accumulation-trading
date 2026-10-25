"""POSIX advisory lock preventing two local processes from trading one account."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ProcessFileLock:
    """Hold a non-blocking OS file lock for the lifetime of the application."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("this ProcessFileLock instance already owns its lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"another process holds the trading lock at {self._path}"
                ) from exc
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        os.close(fd)
