"""Cross-process exclusion for backup runs using a persistent lock file."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO

from nimbo.core.errors import LockedError


class RunLock:
    """Hold an exclusive, nonblocking flock on a dedicated file until released."""

    def __init__(self, path: Path) -> None:
        """Bind the lock to a path shared by contenders."""
        self._path = path
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        """Acquire the lock and write the holder PID, or raise LockedError if held.

        Propagate other filesystem errors after closing the opened descriptor.
        """
        if self._handle is not None:
            raise LockedError()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockedError() from None
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        """Close the held descriptor if present and leave the lock file in place."""
        handle = self._handle
        if handle is not None:
            self._handle = None
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> RunLock:
        """Acquire the lock and return it, propagating any acquisition error."""
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the lock even if the context body raised."""
        self.release()


def is_locked(path: Path) -> bool:
    """Probe for an exclusive holder without creating or writing the lock file."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False
