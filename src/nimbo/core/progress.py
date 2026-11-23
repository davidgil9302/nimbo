"""Collect thread-safe run progress and publish snapshots for local readers."""

from __future__ import annotations

import math
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from nimbo.core.fsutil import atomic_write_text, read_json
from nimbo.core.model import ActiveTransfer, Progress, rel_top, utc_now_iso

_HISTORY_SIZE = 60
_SPEED_ALPHA = 0.3


class ProgressTracker:
    """Track operation counts, active transfers and throughput for one run."""

    def __init__(
        self,
        path: Path,
        run_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], str] = utc_now_iso,
        min_interval: float = 0.5,
    ) -> None:
        """Initialize in preflight using a monotonic clock and a wall-time formatter."""
        self._path = path
        self._clock = clock
        self._wall = wall
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._closed = False
        started = wall()
        self._state = Progress(run_id, os.getpid(), "preflight", started, started)
        self._active: dict[str, ActiveTransfer] = {}
        self._last_write: float | None = None
        self._sample_at = clock()
        self._sample_bytes = 0
        self._speed = 0.0
        self._history: deque[float] = deque(maxlen=_HISTORY_SIZE)

    def phase(
        self,
        name: str,
        *,
        ops_total: int | None = None,
        bytes_total: int | None = None,
        folder: str | None = None,
    ) -> None:
        """Set the phase and folder, resetting each counter whose new total is supplied."""
        with self._lock:
            self._state.phase = name
            self._state.folder = folder
            if ops_total is not None:
                self._state.ops_total = ops_total
                self._state.ops_done = 0
            now = self._clock()
            if bytes_total is not None:
                self._state.bytes_total = bytes_total
                self._state.bytes_done = 0
                self._sample_at = now
                self._sample_bytes = 0
                self._speed = 0.0
                self._history.clear()
                self._active.clear()
            self._flush(now)

    def op_done(self, rel: str) -> None:
        """Count a completed operation and remove its transfer if active."""
        with self._lock:
            self._state.ops_done += 1
            self._active.pop(rel, None)
            self._flush(self._clock())

    def transfer(self, rel: str, delta: int, total: int) -> None:
        """Add a byte delta to the active path and the phase's total bytes sent."""
        with self._lock:
            previous = self._active.pop(rel, None)
            done = previous.done if previous is not None else 0
            self._active[rel] = ActiveTransfer(rel, done + delta, total)
            self._state.bytes_done += delta
            self._sample_bytes += delta
            self._flush(self._clock())

    def transfer_done(self, rel: str) -> None:
        """Remove an active transfer without affecting operation or byte counts."""
        with self._lock:
            self._active.pop(rel, None)
            self._flush(self._clock())

    def _sample_speed(self, now: float) -> None:
        seconds = int(now - self._sample_at)
        if seconds < 1:
            return
        # Spread the measured bytes over the completed seconds between observations.
        sample = self._sample_bytes / seconds
        self._sample_at += seconds
        self._sample_bytes = 0
        if not self._history:
            self._speed = sample
        skipped = max(seconds - _HISTORY_SIZE, 0)
        if skipped:
            self._speed = sample + (self._speed - sample) * (1 - _SPEED_ALPHA) ** skipped
        for _ in range(seconds - skipped):
            self._speed += _SPEED_ALPHA * (sample - self._speed)
            self._history.append(self._speed)

    def _snapshot(self) -> Progress:
        active = [replace(transfer) for transfer in self._active.values()]
        folder = self._state.folder
        if folder is None and active:
            folder = rel_top(active[-1].rel)
        remaining = max(self._state.bytes_total - self._state.bytes_done, 0)
        eta = remaining / self._speed if self._speed else math.inf
        return replace(
            self._state,
            updated_at=self._wall(),
            folder=folder,
            speed_bps=self._speed,
            eta_s=math.ceil(eta) if math.isfinite(eta) else None,
            active=active,
            speed_history=list(self._history),
        )

    def snapshot(self) -> Progress:
        """Return a detached snapshot of the latest in-memory progress."""
        with self._lock:
            self._sample_speed(self._clock())
            return self._snapshot()

    def _flush(self, now: float, *, force: bool = False) -> None:
        if self._closed:
            return
        self._sample_speed(now)
        if (
            not force
            and self._last_write is not None
            and now - self._last_write < self._min_interval
        ):
            return
        atomic_write_text(self._path, self._snapshot().to_json() + "\n")
        self._last_write = self._clock()

    def flush(self, *, force: bool = False) -> None:
        """Publish an atomic snapshot when due, or immediately with force=True.

        Propagate filesystem errors. A closed tracker never publishes again.
        """
        with self._lock:
            self._flush(self._clock(), force=force)

    def close(self) -> None:
        """Stop publishing and remove the snapshot, propagating filesystem errors."""
        with self._lock:
            if not self._closed:
                self._path.unlink(missing_ok=True)
                self._closed = True


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _speed(value: object) -> bool:
    return (type(value) is int or type(value) is float) and value >= 0 and math.isfinite(value)


def _valid(state: Progress) -> bool:
    return (
        type(state.pid) is int
        and state.pid > 0
        and all(
            _count(value)
            for value in (
                state.run_id,
                state.ops_total,
                state.ops_done,
                state.bytes_total,
                state.bytes_done,
            )
        )
        and all(
            isinstance(value, str) for value in (state.phase, state.started_at, state.updated_at)
        )
        and (state.folder is None or isinstance(state.folder, str))
        and (state.eta_s is None or _count(state.eta_s))
        and _speed(state.speed_bps)
        and len(state.speed_history) <= _HISTORY_SIZE
        and all(_speed(value) for value in state.speed_history)
        and all(
            isinstance(transfer.rel, str) and _count(transfer.done) and _count(transfer.total)
            for transfer in state.active
        )
    )


def read_progress(path: Path) -> Progress | None:
    """Read a valid snapshot for a live PID, or return None if absent, invalid or stale."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        data = read_json(path)
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("active", []), list) or not isinstance(
            data.get("speed_history", []), list
        ):
            return None
        data["active"] = [ActiveTransfer(**item) for item in data.get("active", [])]
        state = Progress(**data)
        if not _valid(state):
            return None
        # PermissionError confirms that the process exists under another user.
        with suppress(PermissionError):
            os.kill(state.pid, 0)
    except (OSError, OverflowError, RecursionError, TypeError):
        return None
    return state
