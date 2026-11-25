"""Deterministic, metadata-only scans of configured local directories."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Generator, Sequence
from operator import attrgetter
from pathlib import Path
from typing import Protocol

from nimbo.core.errors import Stopped
from nimbo.core.model import NOBACKUP_NAME, LocalEntry, ScanProblem, ScanResult, nfc, rel_join

_SURROGATE = re.compile(r"[\ud800-\udfff]")
_PROGRESS_INTERVAL = 1000
_DIR_FLAGS = os.O_NOFOLLOW | os.O_DIRECTORY


class MatcherLike(Protocol):
    """Exclusion policy used by the scanner for NFC relative paths."""

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """Return whether the file or directory at ``rel`` is excluded."""
        ...


def _unreadable(result: ScanResult, rel: str, error: OSError) -> None:
    detail = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    result.problems.append(ScanProblem("unreadable", rel, detail))
    result.protected.append(rel)


def _scan_directory(
    parent_fd: int,
    name: str,
    entry: LocalEntry,
    before: os.stat_result,
    matcher: MatcherLike,
    result: ScanResult,
    visit: Callable[[], None],
    *,
    configured: bool = False,
) -> Generator[tuple[int, str, LocalEntry, os.stat_result], None, None]:
    try:
        fd = os.open(name, os.O_RDONLY | _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        _unreadable(result, entry.rel, error)
        return

    try:
        try:
            if not os.path.samestat(before, os.fstat(fd)):
                raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))
            try:
                marker = os.stat(NOBACKUP_NAME, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(marker.st_mode):
                    result.excluded += 1
                    result.protected.append(entry.rel)
                    if not configured:
                        del result.entries[entry.rel]
                    return
            with os.scandir(fd) as children:
                ordered = sorted(children, key=attrgetter("name"))
        except OSError as error:
            _unreadable(result, entry.rel, error)
            return

        sibling_counts = Counter(nfc(child.name) for child in ordered)
        subdirs = []
        for child in ordered:
            visit()
            name = child.name
            if _SURROGATE.search(name):
                result.problems.append(ScanProblem("invalid-name", rel_join(entry.rel, name)))
                continue

            canonical = nfc(name)
            rel = rel_join(entry.rel, canonical)
            if sibling_counts[canonical] > 1:
                result.problems.append(ScanProblem("nfc-collision", rel_join(entry.disk_rel, name)))
                result.protected.append(rel)
                continue

            try:
                info = child.stat(follow_symlinks=False)
            except OSError as error:
                _unreadable(result, rel, error)
                continue

            disk = (
                rel_join(entry.disk_rel, name)
                if entry.disk is not None or name != canonical
                else None
            )
            mode = info.st_mode
            if stat.S_ISDIR(mode):
                if matcher.excluded(rel, True):
                    result.excluded += 1
                else:
                    subdir = LocalEntry(rel, True, 0, info.st_mtime_ns, disk=disk)
                    result.entries[rel] = subdir
                    subdirs.append((fd, name, subdir, info))
            elif stat.S_ISREG(mode):
                if matcher.excluded(rel, False):
                    result.excluded += 1
                else:
                    result.entries[rel] = LocalEntry(
                        rel,
                        False,
                        info.st_size,
                        info.st_mtime_ns,
                        disk=disk,
                        ino=info.st_ino,
                        ctime_ns=info.st_ctime_ns,
                    )
            elif stat.S_ISLNK(mode):
                result.problems.append(ScanProblem("symlink", rel))
            else:
                result.problems.append(ScanProblem("special-file", rel))

        yield from subdirs
    finally:
        os.close(fd)


def _scan_folder(
    root_fd: int,
    folder: str,
    matcher: MatcherLike,
    result: ScanResult,
    visit: Callable[[], None],
) -> None:
    try:
        info = os.stat(folder, dir_fd=root_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        result.missing_folders.append(folder)
        return
    except OSError as error:
        _unreadable(result, folder, error)
        return

    if not stat.S_ISDIR(info.st_mode):
        result.missing_folders.append(folder)
        if stat.S_ISLNK(info.st_mode):
            result.problems.append(ScanProblem("symlink", folder))
        return

    initial_entries = len(result.entries)
    initial_problems = len(result.problems)
    entry = LocalEntry(folder, True, 0, info.st_mtime_ns)
    result.entries[folder] = entry
    if matcher.excluded(folder, True):
        result.excluded += 1
    else:
        # Keep only the current branch's descriptors open, even with many siblings.
        pending = [
            _scan_directory(root_fd, folder, entry, info, matcher, result, visit, configured=True)
        ]
        try:
            while pending:
                child = next(pending[-1], None)
                if child is None:
                    pending.pop()
                else:
                    pending.append(_scan_directory(*child, matcher, result, visit))
        finally:
            for directory in reversed(pending):
                directory.close()

    if (
        len(result.entries) == initial_entries + 1
        and len(result.problems) == initial_problems
        and folder not in result.protected
    ):
        result.empty_folders.append(folder)


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

    def visit() -> None:
        nonlocal visited
        visited += 1
        if visited % _PROGRESS_INTERVAL == 0:
            if on_progress is not None:
                on_progress(visited)
            if should_stop is not None and should_stop():
                raise Stopped

    try:
        # The configured root may be an alias and needs only search permission.
        root_fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        result.missing_folders.extend(folders)
    except OSError as error:
        for folder in folders:
            _unreadable(result, folder, error)
    else:
        try:
            for folder in folders:
                _scan_folder(root_fd, folder, matcher, result, visit)
        finally:
            os.close(root_fd)

    result.protected = sorted(set(result.protected))
    return result
