"""Deterministic, metadata-only scans of configured local directories."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Callable, Sequence
from operator import attrgetter
from pathlib import Path
from typing import Protocol

from nimbo.core.errors import Stopped
from nimbo.core.model import NOBACKUP_NAME, LocalEntry, ScanProblem, ScanResult, nfc, rel_join

_SURROGATE = re.compile(r"[\ud800-\udfff]")
_PROGRESS_INTERVAL = 1000


class MatcherLike(Protocol):
    """Exclusion policy used by the scanner for NFC relative paths."""

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """Return whether the file or directory at ``rel`` is excluded."""
        ...


def _unreadable(result: ScanResult, rel: str, error: OSError) -> None:
    detail = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    result.problems.append(ScanProblem("unreadable", rel, detail))
    result.protected.append(rel)


def _schedule_directory(
    path: Path,
    entry: LocalEntry,
    matcher: MatcherLike,
    pending: list[LocalEntry],
    result: ScanResult,
    *,
    configured: bool = False,
) -> None:
    excluded = matcher.excluded(entry.rel, True)
    if not excluded:
        try:
            excluded = stat.S_ISREG((path / NOBACKUP_NAME).lstat().st_mode)
            if excluded:
                result.protected.append(entry.rel)
        except FileNotFoundError:
            pass
        except OSError as error:
            _unreadable(result, entry.rel, error)
            result.entries[entry.rel] = entry
            return

    if excluded:
        result.excluded += 1
    else:
        pending.append(entry)
    if configured or not excluded:
        result.entries[entry.rel] = entry


def scan(
    root: Path,
    folders: Sequence[str],
    matcher: MatcherLike,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Scan the named NFC folders under ``root`` for entries, problems and protected paths.

    Record the physical relative path on entries whose spelling differs from NFC.
    Call ``on_progress`` and check ``should_stop`` every 1,000 visited children.
    Raise ``Stopped`` when a checkpoint observes a stop request.
    """
    result = ScanResult(entries={})
    visited = 0
    for folder in folders:
        path = root / folder
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            result.missing_folders.append(folder)
            continue
        except OSError as error:
            _unreadable(result, folder, error)
            continue

        if not stat.S_ISDIR(info.st_mode):
            result.missing_folders.append(folder)
            if stat.S_ISLNK(info.st_mode):
                result.problems.append(ScanProblem("symlink", folder))
            continue

        initial_entries = len(result.entries)
        pending: list[LocalEntry] = []
        _schedule_directory(
            path,
            LocalEntry(folder, True, 0, info.st_mtime_ns),
            matcher,
            pending,
            result,
            configured=True,
        )
        while pending:
            parent = pending.pop()
            disk_parent = parent.disk_rel
            try:
                with os.scandir(root / disk_parent) as children:
                    ordered = sorted(children, key=attrgetter("name"))
            except OSError as error:
                _unreadable(result, parent.rel, error)
                continue

            seen: set[str] = set()
            subdirs: list[LocalEntry] = []
            for child in ordered:
                visited += 1
                if visited % _PROGRESS_INTERVAL == 0:
                    if on_progress is not None:
                        on_progress(visited)
                    if should_stop is not None and should_stop():
                        raise Stopped

                name = child.name
                if _SURROGATE.search(name):
                    result.problems.append(ScanProblem("invalid-name", rel_join(parent.rel, name)))
                    continue

                canonical = nfc(name)
                rel = rel_join(parent.rel, canonical)
                if rel in seen:
                    result.problems.append(ScanProblem("nfc-collision", rel, name))
                    continue
                seen.add(rel)

                try:
                    info = child.stat(follow_symlinks=False)
                except OSError as error:
                    _unreadable(result, rel, error)
                    continue

                disk = (
                    rel_join(disk_parent, name)
                    if parent.disk is not None or name != canonical
                    else None
                )
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    _schedule_directory(
                        Path(child.path),
                        LocalEntry(rel, True, 0, info.st_mtime_ns, disk=disk),
                        matcher,
                        subdirs,
                        result,
                    )
                elif stat.S_ISREG(mode):
                    if matcher.excluded(rel, False):
                        result.excluded += 1
                    else:
                        result.entries[rel] = LocalEntry(
                            rel, False, info.st_size, info.st_mtime_ns, disk=disk
                        )
                elif stat.S_ISLNK(mode):
                    result.problems.append(ScanProblem("symlink", rel))
                else:
                    result.problems.append(ScanProblem("special-file", rel))

            pending.extend(reversed(subdirs))

        if len(result.entries) == initial_entries + 1 and folder not in result.protected:
            result.empty_folders.append(folder)

    result.protected = sorted(set(result.protected))
    return result
