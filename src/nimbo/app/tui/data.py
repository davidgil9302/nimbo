"""Read the service's published state without opening a writable index."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from nimbo.app import service
from nimbo.core.config import Config, load_config
from nimbo.core.index import META_QUOTA_LIMIT, META_QUOTA_USED, Index
from nimbo.core.lock import is_locked
from nimbo.core.model import FolderStat, Progress, RunResult, RunSummary, rel_top
from nimbo.core.paths import Paths
from nimbo.core.progress import read_progress

FolderState = Literal[
    "up_to_date", "changes", "running", "waiting", "no_disk", "error", "paused", "unknown"
]


@dataclass(frozen=True, slots=True)
class FolderView:
    """One configured folder and the coverage recorded for it by the engine."""

    name: str
    state: FolderState
    last_ok: datetime | None
    pending_files: int | None
    pending_bytes: int | None
    coverage: float | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A display-ready, best-effort observation of an installed backup."""

    configured: bool
    service: str
    target_label: str
    last_run: RunSummary | None
    next_run: datetime | None
    quota_used: int | None
    quota_total: int | None
    folders: tuple[FolderView, ...]
    progress: Progress | None
    coverage: float | None
    needs_confirmation: bool
    errors: tuple[str, ...]
    running: bool = False
    language: str | None = None
    recent_runs: tuple[RunSummary, ...] = ()


def local_now() -> datetime:
    """Return an aware timestamp in the host's local timezone."""
    return datetime.now().astimezone()


def parse_time(value: str | None) -> datetime | None:
    """Parse an engine timestamp, yielding None for missing or malformed values."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coverage(total: int, pending: int, pending_files: int) -> float:
    if total <= 0:
        return float(pending_files == 0)
    return min(max(total - pending, 0), total) / total


def _count(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Invalid counter")
    return max(0, value)


def _quota(value: str | None) -> int | None:
    if value is None:
        return None
    size = int(value)
    if not 0 <= size <= 2**63 - 1:
        raise ValueError("Invalid Drive quota")
    return size


def _index_exists(paths: Paths) -> bool:
    try:
        metadata = paths.db_file.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Index is not a regular file")
    return True


def _check_run(run: RunSummary) -> None:
    # Deserialization checks the result enum, but leaves the other JSON fields untyped.
    if not all(
        isinstance(value, str)
        for value in (run.reason, run.started_at, run.finished_at, run.trigger)
    ) or (run.fingerprint is not None and not isinstance(run.fingerprint, str)):
        raise ValueError("Invalid run metadata")
    for count in (run.uploaded, run.uploaded_bytes, run.updated, run.moved, run.trashed):
        _count(count)
    if run.run_id is not None:
        _count(run.run_id)
    if not isinstance(run.trash_preview, list) or not all(
        isinstance(path, str) for path in run.trash_preview
    ):
        raise ValueError("Invalid deletion preview")
    for path, code in run.failed:
        if not isinstance(path, str) or not isinstance(code, str):
            raise ValueError("Invalid operation failure")


def load_runs(paths: Paths, *, limit: int = 50) -> list[RunSummary]:
    """Read recent finished runs, propagating a malformed or unreadable index."""
    if not _index_exists(paths):
        return []
    with closing(Index.open_readonly(paths.db_file)) as index:
        runs = index.list_runs(limit=limit)
    for run in runs:
        _check_run(run)
    return runs


def _index_data(
    paths: Paths,
) -> tuple[tuple[RunSummary, ...], dict[str, FolderStat], int | None, int | None]:
    if not _index_exists(paths):
        return (), {}, None, None
    with closing(Index.open_readonly(paths.db_file)) as index:
        runs = tuple(index.list_runs(limit=4))
        for run in runs:
            _check_run(run)
        stats = index.folder_stats()
        for row in stats.values():
            _count(row.bytes_local)
            _count(row.bytes_pending)
            _count(row.files_pending)
        return (
            runs,
            stats,
            _quota(index.get_meta(META_QUOTA_USED)),
            _quota(index.get_meta(META_QUOTA_LIMIT)),
        )


def _folder_views(
    cfg: Config,
    run: RunSummary | None,
    stats: dict[str, FolderStat],
    progress: Progress | None,
    running: bool,
) -> tuple[tuple[FolderView, ...], float | None]:
    root = cfg.source.root
    no_disk = cfg.source.require_mount and not os.path.ismount(root)
    # The tracker can report multiple transfers in different folders at once.
    moving = {rel_top(item.rel) for item in progress.active} if progress else set()
    if progress and progress.folder:
        moving.add(progress.folder)
    failed = {rel_top(path) for path, _ in run.failed} if run else set()

    folders: list[FolderView] = []
    active_stats: list[tuple[int, int, int]] = []
    for name in cfg.source.folders:
        row = stats.get(name)
        pending_files = _count(row.files_pending) if row else None
        pending_bytes = _count(row.bytes_pending) if row else None
        paused = name in cfg.source.paused
        missing = False
        if not paused and not no_disk:
            try:
                missing = not stat.S_ISDIR((root / name).lstat().st_mode)
            except OSError:
                missing = True
        state: FolderState
        if paused:
            state = "paused"
        elif name in moving:
            state = "running"
        elif no_disk:
            state = "no_disk"
        elif missing or (name in failed and not running):
            state = "error"
        elif running and (row is None or pending_files or pending_bytes):
            state = "waiting"
        elif row is None:
            state = "unknown"
        elif pending_files or pending_bytes:
            state = "changes"
        else:
            state = "up_to_date"

        coverage = None
        if row is not None and pending_files is not None and pending_bytes is not None:
            total = _count(row.bytes_local)
            coverage = _coverage(total, pending_bytes, pending_files)
            if not paused and not no_disk and not missing:
                active_stats.append((total, min(pending_bytes, total), pending_files))
        folders.append(
            FolderView(
                name,
                state,
                parse_time(row.last_ok) if row else None,
                pending_files,
                pending_bytes,
                coverage,
            )
        )

    overall = None
    if active_stats and len(active_stats) == len(cfg.active_folders):
        overall = _coverage(
            sum(total for total, _, _ in active_stats),
            sum(pending for _, pending, _ in active_stats),
            sum(files for _, _, files in active_stats),
        )
    return tuple(folders), overall


def load_snapshot(
    paths: Paths,
    *,
    service_state: Callable[[], service.ServiceState] = service.service_state,
    now: Callable[[], datetime] = local_now,
) -> Snapshot:
    """Observe independent sources, returning localized error keys for unreadable inputs."""
    errors: list[str] = []
    try:
        if not stat.S_ISREG(paths.config_file.stat().st_mode):
            raise ValueError("Configuration is not a regular file")
        cfg = load_config(paths.config_file)
    except FileNotFoundError:
        cfg = None
    except Exception:  # noqa: BLE001 - an invalid config must still allow setup and diagnostics
        cfg = None
        errors.append("tui.error.config")
    if cfg is None:
        return Snapshot(False, "", "", None, None, None, None, (), None, None, False, tuple(errors))

    try:
        state = service_state()
        if not isinstance(state, service.ServiceState):
            raise TypeError("Invalid service state")
    except Exception:  # noqa: BLE001 - the service probe is independent of stored state
        state = service.ServiceState(False, False, False, None)
        errors.append("tui.error.service")

    try:
        runs, stats, used, limit = _index_data(paths)
    except Exception:  # noqa: BLE001 - SQLite and JSON can fail independently of live progress
        runs, stats, used, limit = (), {}, None, None
        errors.append("tui.error.index")
    last_run = runs[0] if runs else None

    progress = None
    try:
        paths.progress_file.lstat()
        progress = read_progress(paths.progress_file)
        if progress is None:
            # Atomic removal is normal when a backup finishes between these reads.
            paths.progress_file.lstat()
            errors.append("tui.error.progress")
        elif last_run and last_run.run_id is not None and progress.run_id <= last_run.run_id:
            progress = None
        elif not state.running and not is_locked(paths.lock_file):
            # A PID alone can be reused after a crash. A direct CLI run holds the lock.
            updated = parse_time(progress.updated_at)
            observed = now()
            if updated is None or not timedelta(0) <= observed - updated <= timedelta(seconds=5):
                progress = None
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - malformed progress must never break the dashboard
        progress = None
        errors.append("tui.error.progress")

    running = state.running or progress is not None
    try:
        folders, coverage = _folder_views(cfg, last_run, stats, progress, running)
    except (OSError, ValueError):
        folders, coverage = (), None
        errors.append("tui.error.source")

    return Snapshot(
        True,
        cfg.service,
        cfg.target.label,
        last_run,
        state.next_run if state.timer_enabled else None,
        used,
        limit,
        folders,
        progress,
        coverage,
        not running and last_run is not None and last_run.result is RunResult.NEEDS_CONFIRMATION,
        tuple(errors),
        running,
        cfg.language,
        runs,
    )
