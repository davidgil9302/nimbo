"""Filesystem tests for the local scan contract."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest

from nimbo.core.errors import Stopped
from nimbo.core.model import LocalEntry, ScanProblem
from nimbo.core.scanner import scan


class _Matcher:
    def __init__(self, *, dirs: tuple[str, ...] = (), basenames: tuple[str, ...] = ()) -> None:
        self.dirs = dirs
        self.basenames = basenames

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """Match exact directories and basename patterns at any depth."""
        return (is_dir and rel in self.dirs) or any(
            fnmatchcase(rel.rpartition("/")[2], pattern) for pattern in self.basenames
        )


def _write(root: Path, rel: str, content: bytes = b"") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scans_nested_tree_with_sizes_and_mtimes(source_root: Path) -> None:
    """Collect exact times, sizes and regular-file identities beneath a configured folder."""
    data = _write(source_root, "Docs/nested/data", b"123")
    _write(source_root, "Docs/zero")
    mtime_ns = 1_700_000_000_123_456_789
    os.utime(data, ns=(mtime_ns, mtime_ns))
    _write(source_root, "Unconfigured/outside")

    result = scan(source_root, ["Docs"], _Matcher())

    metadata = {
        "Docs": (True, 0),
        "Docs/nested": (True, 0),
        "Docs/nested/data": (False, 3),
        "Docs/zero": (False, 0),
    }
    assert set(result.entries) == metadata.keys()
    for rel, (is_dir, size) in metadata.items():
        info = (source_root / rel).stat()
        assert result.entries[rel] == LocalEntry(
            rel,
            is_dir,
            size,
            info.st_mtime_ns,
            ino=None if is_dir else info.st_ino,
            ctime_ns=None if is_dir else info.st_ctime_ns,
        )
    assert result.problems == []
    assert result.missing_folders == []
    assert result.empty_folders == []


def test_folder_entry_emitted(source_root: Path) -> None:
    """Give the configured folder its own directory entry."""
    folder = source_root / "Docs"
    folder.mkdir()

    result = scan(source_root, ["Docs"], _Matcher())

    assert result.entries == {"Docs": LocalEntry("Docs", True, 0, folder.stat().st_mtime_ns)}


def test_missing_and_non_dir_folders_reported(source_root: Path) -> None:
    """Report absent paths and regular files as missing configured folders."""
    _write(source_root, "NotADir")
    _write(source_root, "Docs/data")

    result = scan(source_root, ["Missing", "Docs", "NotADir"], _Matcher())

    assert result.missing_folders == ["Missing", "NotADir"]
    assert set(result.entries) == {"Docs", "Docs/data"}


def test_empty_folder_reported(source_root: Path) -> None:
    """List configured folders with no included descendants as empty."""
    (source_root / "Empty").mkdir()
    _write(source_root, "OnlyExcluded/ignore.tmp")
    (source_root / "WithSubdir/empty").mkdir(parents=True)

    result = scan(
        source_root, ["Empty", "OnlyExcluded", "WithSubdir"], _Matcher(basenames=("*.tmp",))
    )

    assert result.empty_folders == ["Empty", "OnlyExcluded"]
    assert result.excluded == 1


def test_excluded_dirs_not_traversed(source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prune an excluded subtree before any directory listing."""
    excluded = _write(source_root, "Docs/skip/child/data").parent.parent
    _write(source_root, "Docs/keep")
    scandir = os.scandir

    def guard(path: int) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        assert not os.path.samestat(os.fstat(path), excluded.stat())
        return scandir(path)

    monkeypatch.setattr(os, "scandir", guard)
    result = scan(source_root, ["Docs"], _Matcher(dirs=("Docs/skip",)))

    assert set(result.entries) == {"Docs", "Docs/keep"}
    assert result.excluded == 1
    assert result.problems == []
    assert result.protected == []


def test_excluded_files_counted(source_root: Path) -> None:
    """Count each matched regular file across directories."""
    _write(source_root, "Docs/ignore.tmp")
    _write(source_root, "Docs/nested/also.tmp")
    _write(source_root, "Docs/nested/keep.txt")

    result = scan(source_root, ["Docs"], _Matcher(basenames=("*.tmp",)))

    assert set(result.entries) == {"Docs", "Docs/nested", "Docs/nested/keep.txt"}
    assert result.excluded == 2


def test_nobackup_marker_excludes_dir(source_root: Path) -> None:
    """Exclude the marked directory and all its descendants as one item."""
    _write(source_root, "Docs/private/.nobackup")
    _write(source_root, "Docs/private/a")
    _write(source_root, "Docs/private/sub/b")
    _write(source_root, "Docs/keep")

    result = scan(source_root, ["Docs"], _Matcher())

    assert set(result.entries) == {"Docs", "Docs/keep"}
    assert result.excluded == 1
    assert result.problems == []


def test_nobackup_dir_is_protected(source_root: Path) -> None:
    """Protect marked directories by NFC path, in sorted order and once per path."""
    _write(source_root, "Z/Cafe\u0301/.nobackup")
    _write(source_root, "Z/Cafe\u0301/private")
    _write(source_root, "A/private/.nobackup")
    _write(source_root, "A/skip/.nobackup")
    _write(source_root, "A/keep")

    result = scan(source_root, ["Z", "A", "Z"], _Matcher(dirs=("A/skip",)))

    assert result.protected == ["A/private", "Z/Caf\u00e9"]
    assert set(result.entries) == {"A", "A/keep", "Z"}
    assert result.problems == []


def test_configured_folder_with_nobackup_is_protected_not_empty(source_root: Path) -> None:
    """Protect the configured folder itself and prune its marked contents."""
    _write(source_root, "Docs/.nobackup")
    _write(source_root, "Docs/private")

    result = scan(source_root, ["Docs"], _Matcher())

    assert set(result.entries) == {"Docs"}
    assert result.excluded == 1
    assert result.empty_folders == []
    assert result.missing_folders == []
    assert result.protected == ["Docs"]


def test_symlinks_skipped_and_reported(source_root: Path) -> None:
    """Detect file, directory and dangling links without following them."""
    target_file = _write(source_root, "Other/target")
    _write(source_root, "Docs/keep")
    folder = source_root / "Docs"
    (folder / "file-link").symlink_to(target_file)
    (folder / "dir-link").symlink_to(target_file.parent, target_is_directory=True)
    (folder / "dangling-link").symlink_to(source_root / "Other/missing")

    result = scan(source_root, ["Docs"], _Matcher(basenames=("*-link",)))

    assert set(result.entries) == {"Docs", "Docs/keep"}
    assert result.problems == [
        ScanProblem("symlink", f"Docs/{name}")
        for name in ("dangling-link", "dir-link", "file-link")
    ]
    assert result.excluded == 0


def test_configured_folder_symlink_is_not_walked(source_root: Path) -> None:
    """Treat a root-level configured symlink as a missing directory and report the link."""
    target = _write(source_root, "Other/target").parent
    (source_root / "Docs").symlink_to(target, target_is_directory=True)

    result = scan(source_root, ["Docs"], _Matcher())

    assert result.entries == {}
    assert result.missing_folders == ["Docs"]
    assert result.problems == [ScanProblem("symlink", "Docs")]


def test_fifo_reported_as_special(source_root: Path) -> None:
    """Skip a FIFO without opening it."""
    _write(source_root, "Docs/keep")
    os.mkfifo(source_root / "Docs/pipe")

    result = scan(source_root, ["Docs"], _Matcher())

    assert set(result.entries) == {"Docs", "Docs/keep"}
    assert result.problems == [ScanProblem("special-file", "Docs/pipe")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list directories regardless of mode")
@pytest.mark.parametrize("mode", [0o000, 0o100])
def test_unreadable_dir_reported_and_scan_continues(source_root: Path, mode: int) -> None:
    """Record EACCES and continue with other paths when a directory cannot be listed."""
    unreadable = _write(source_root, "Docs/blocked/hidden").parent
    _write(source_root, "Docs/keep")
    unreadable.chmod(mode)

    try:
        result = scan(source_root, ["Docs"], _Matcher())
    finally:
        unreadable.chmod(0o700)

    assert result.problems == [ScanProblem("unreadable", "Docs/blocked", "EACCES")]
    assert set(result.entries) == {"Docs", "Docs/blocked", "Docs/keep"}
    assert result.protected == ["Docs/blocked"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list directories regardless of mode")
def test_unreadable_dir_is_protected(source_root: Path) -> None:
    """Protect a configured directory whose contents cannot be listed."""
    unreadable = _write(source_root, "Docs/hidden").parent
    _write(source_root, "Other/keep")
    unreadable.chmod(0o100)

    try:
        result = scan(source_root, ["Docs", "Other"], _Matcher())
    finally:
        unreadable.chmod(0o700)

    assert result.protected == ["Docs"]
    assert result.problems == [ScanProblem("unreadable", "Docs", "EACCES")]
    assert set(result.entries) == {"Docs", "Other", "Other/keep"}
    assert result.empty_folders == []
    assert result.missing_folders == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can stat paths without search permission")
def test_unreadable_configured_folder_is_distinct_from_missing(source_root: Path) -> None:
    """Record a permission failure during the configured folder's initial stat."""
    _write(source_root, "Docs/data")
    source_root.chmod(0o600)

    try:
        result = scan(source_root, ["Docs"], _Matcher())
    finally:
        source_root.chmod(0o700)

    assert result.problems == [ScanProblem("unreadable", "Docs", "EACCES")]
    assert result.entries == {}
    assert result.missing_folders == []
    assert result.protected == ["Docs"]


def test_invalid_utf8_name_reported(source_root: Path) -> None:
    """Reject surrogate-escaped names before traversing the directory."""
    _write(source_root, "Docs/keep")
    bad_dir = os.path.join(os.fsencode(source_root / "Docs"), b"bad\xff")
    os.mkdir(bad_dir)
    os.mkdir(os.path.join(bad_dir, b"hidden"))

    result = scan(source_root, ["Docs"], _Matcher())

    assert set(result.entries) == {"Docs", "Docs/keep"}
    assert result.problems == [ScanProblem("invalid-name", "Docs/bad\udcff")]
    assert result.protected == []


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_nfc_collision_protects_all_spellings(source_root: Path, suffix: str) -> None:
    """Skip both spellings without walking either colliding directory."""
    composed = "Docs/é"
    decomposed = "Docs/e\u0301"
    _write(source_root, composed + suffix)
    _write(source_root, decomposed + suffix)

    result = scan(source_root, ["Docs"], _Matcher())

    assert result.entries.keys() == {"Docs"}
    assert result.problems == [
        ScanProblem("nfc-collision", decomposed),
        ScanProblem("nfc-collision", composed),
    ]
    assert result.protected == [composed]
    assert result.empty_folders == []


def test_disk_path_set_for_nfd_names(source_root: Path) -> None:
    """Record the full disk path for NFD files, directories and their descendants."""
    _write(source_root, "Docs/re\u0301sume\u0301.txt")
    _write(source_root, "Docs/Cafe\u0301/child.txt")

    result = scan(source_root, ["Docs"], _Matcher())

    assert {rel: entry.disk for rel, entry in result.entries.items()} == {
        "Docs": None,
        "Docs/r\u00e9sum\u00e9.txt": "Docs/re\u0301sume\u0301.txt",
        "Docs/Caf\u00e9": "Docs/Cafe\u0301",
        "Docs/Caf\u00e9/child.txt": "Docs/Cafe\u0301/child.txt",
    }
    assert (source_root / result.entries["Docs/Caf\u00e9"].disk_rel).is_dir()
    assert all((source_root / entry.disk_rel).exists() for entry in result.entries.values())


def test_rel_paths_are_nfc(source_root: Path) -> None:
    """Normalize every relative component before matching or emitting entries."""
    _write(source_root, "Docs/Cafe\u0301/re\u0301sume\u0301")
    _write(source_root, "Docs/Cafe\u0301/noe\u0308l.tmp")

    result = scan(source_root, ["Docs"], _Matcher(basenames=("no\u00ebl.tmp",)))

    assert set(result.entries) == {"Docs", "Docs/Caf\u00e9", "Docs/Caf\u00e9/r\u00e9sum\u00e9"}
    assert all(rel == entry.rel for rel, entry in result.entries.items())
    assert result.excluded == 1


def test_scan_is_deterministic(source_root: Path) -> None:
    """Return the same order for the same tree regardless of insertion order."""
    names = ("Docs/Z/file", "Docs/c", "Docs/a", "Docs/Z/aleph", "Docs/B")
    first, second = source_root / "first", source_root / "second"
    for rel in names:
        _write(first, rel)
    for rel in reversed(names):
        _write(second, rel)

    first_order = list(scan(first, ["Docs"], _Matcher()).entries)
    second_order = list(scan(second, ["Docs"], _Matcher()).entries)

    assert first_order == second_order
    assert [rel for rel in first_order if rel.count("/") == 1] == [
        "Docs/B",
        "Docs/Z",
        "Docs/a",
        "Docs/c",
    ]
    assert first_order.index("Docs/Z") < first_order.index("Docs/Z/aleph")
    assert first_order.index("Docs/Z/aleph") < first_order.index("Docs/Z/file")


def _make_files(folder: Path, count: int) -> None:
    folder.mkdir()
    for number in range(count):
        (folder / f"{number:05d}.txt").touch()


def test_should_stop_raises_stopped(source_root: Path) -> None:
    """Raise Stopped at the first requested 1000-entry checkpoint."""
    _make_files(source_root / "Docs", 2100)
    progress: list[int] = []

    with pytest.raises(Stopped):
        scan(
            source_root, ["Docs"], _Matcher(), should_stop=lambda: True, on_progress=progress.append
        )

    assert progress == [1000]


def test_stat_error_is_reported_and_scan_continues(source_root: Path) -> None:
    """Treat a file that vanishes after listing as unreadable and continue the scan."""
    folder = source_root / "Docs"
    _make_files(folder, 1001)

    def remove_next_entry(count: int) -> None:
        assert count == 1000
        (folder / "00999.txt").unlink()

    result = scan(source_root, ["Docs"], _Matcher(), on_progress=remove_next_entry)

    assert result.problems == [ScanProblem("unreadable", "Docs/00999.txt", "ENOENT")]
    assert result.protected == ["Docs/00999.txt"]
    assert "Docs/00999.txt" not in result.entries
    assert "Docs/01000.txt" in result.entries


def test_stat_failure_file_is_protected(source_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Protect the NFC path of a file whose stat fails and keep scanning siblings."""
    blocked = _write(source_root, "Docs/Cafe\u0301.txt")
    _write(source_root, "Docs/keep")
    scandir = os.scandir

    def fail_stat(*, follow_symlinks: bool) -> os.stat_result:
        assert not follow_symlinks
        raise OSError(errno.EIO, "injected stat failure")

    @contextmanager
    def inject_stat_failure(fd: int) -> Iterator[Iterator[os.DirEntry[str] | SimpleNamespace]]:
        in_parent = os.path.samestat(os.fstat(fd), blocked.parent.stat())
        with scandir(fd) as children:
            yield (
                SimpleNamespace(name=child.name, stat=fail_stat)
                if in_parent and child.name == blocked.name
                else child
                for child in children
            )

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", inject_stat_failure)
        result = scan(source_root, ["Docs"], _Matcher())

    assert result.protected == ["Docs/Caf\u00e9.txt"]
    assert result.problems == [ScanProblem("unreadable", "Docs/Caf\u00e9.txt", "EIO")]
    assert set(result.entries) == {"Docs", "Docs/keep"}


def test_progress_callback_called(source_root: Path) -> None:
    """Count visited entries across folders, including excluded files."""
    _make_files(source_root / "A", 750)
    _make_files(source_root / "B", 1250)
    _make_files(source_root / "C", 98)
    progress: list[int] = []
    checks: list[int] = []

    def should_stop() -> bool:
        checks.append(progress[-1])
        return False

    result = scan(
        source_root,
        ["A", "B", "C"],
        _Matcher(basenames=("*.txt",)),
        on_progress=progress.append,
        should_stop=should_stop,
    )

    assert progress == checks == [1000, 2000]
    assert result.excluded == 2098


def test_large_tree_performance(source_root: Path) -> None:
    """Scan 20,000 files in under five seconds, excluding fixture creation."""
    _make_files(source_root / "Docs", 20_000)

    start = perf_counter()
    result = scan(source_root, ["Docs"], _Matcher())
    elapsed = perf_counter() - start

    assert len(result.entries) == 20_001
    assert result.problems == []
    assert elapsed < 5, f"scan took {elapsed:.2f} s"
