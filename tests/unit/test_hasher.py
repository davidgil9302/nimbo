"""Filesystem and cache tests for local MD5 hashing."""

from __future__ import annotations

import builtins
import hashlib
import io
import os
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from nimbo.core.errors import Stopped
from nimbo.core.hasher import LocalHasher, md5_file
from nimbo.core.model import LocalEntry


class _Cache:
    def __init__(self) -> None:
        self.hashes: dict[tuple[str, int, int], str] = {}

    def get_hash(self, rel: str, size: int, mtime_ns: int) -> str | None:
        """Look up a digest by path and observed file metadata."""
        return self.hashes.get((rel, size, mtime_ns))

    def put_hash(self, rel: str, size: int, mtime_ns: int, md5: str) -> None:
        """Store a digest under its path and observed metadata."""
        self.hashes[rel, size, mtime_ns] = md5


def _entry(path: Path, root: Path, *, rel: str | None = None) -> LocalEntry:
    stat_result = path.stat()
    disk = path.relative_to(root).as_posix()
    canonical = disk if rel is None else rel
    return LocalEntry(
        canonical,
        False,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        disk=disk if disk != canonical else None,
    )


def _digest(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _deny_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("the file must not be opened")

    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(io, "open", denied)
    monkeypatch.setattr(os, "open", denied)


@pytest.mark.parametrize("size", [0, 1, (4 << 20) + 3])
def test_md5_file_matches_hashlib(tmp_path: Path, size: int) -> None:
    """Hash empty, single-byte and multi-chunk content exactly as hashlib does."""
    content = (bytes(range(251)) * (size // 251 + 1))[:size]
    path = tmp_path / "file"
    path.write_bytes(content)

    assert md5_file(path) == _digest(content)


def test_cache_hit_avoids_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve cached metadata without opening the file by any common open API."""
    path = tmp_path / "file"
    path.write_bytes(b"cached")
    entry = _entry(path, tmp_path)
    cache = _Cache()
    assert LocalHasher(tmp_path, cache).md5(entry) == _digest(b"cached")
    hasher = LocalHasher(tmp_path, cache)

    with monkeypatch.context() as patch:
        _deny_open(patch)
        assert hasher.md5(entry) == _digest(b"cached")

    assert hasher.hashed_bytes == 0


@pytest.mark.parametrize("change", ["size", "mtime"])
def test_cache_miss_on_change(tmp_path: Path, change: str) -> None:
    """Invalidate a cache hit when either metadata component changes alone."""
    path = tmp_path / "file"
    path.write_bytes(b"a")
    hasher = LocalHasher(tmp_path, _Cache())
    original = _entry(path, tmp_path)
    assert hasher.md5(original) == _digest(b"a")

    content = b"longer" if change == "size" else b"b"
    path.write_bytes(content)
    mtime_ns = original.mtime_ns + (1_000_000_000 if change == "mtime" else 0)
    os.utime(path, ns=(mtime_ns, mtime_ns))

    assert hasher.md5(_entry(path, tmp_path)) == _digest(content)
    assert hasher.hashed_bytes == 1 + len(content)


def test_force_rehash(tmp_path: Path) -> None:
    """Bypass and replace a cached digest for content changed with identical metadata."""
    path = tmp_path / "file"
    path.write_bytes(b"old")
    hasher = LocalHasher(tmp_path, _Cache())
    original = _entry(path, tmp_path)
    assert hasher.md5(original) == _digest(b"old")

    path.write_bytes(b"new")
    os.utime(path, ns=(original.mtime_ns, original.mtime_ns))
    assert _entry(path, tmp_path) == original
    assert hasher.md5(original) == _digest(b"old")
    assert hasher.md5(original, force=True) == _digest(b"new")
    assert hasher.md5(original) == _digest(b"new")
    assert hasher.hashed_bytes == 6


def test_returns_none_if_file_changed_before_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a stale entry before opening or updating the cache."""
    path = tmp_path / "file"
    path.write_bytes(b"old")
    entry = _entry(path, tmp_path)
    path.write_bytes(b"different size")
    cache = _Cache()
    hasher = LocalHasher(tmp_path, cache)

    with monkeypatch.context() as patch:
        _deny_open(patch)
        assert hasher.md5(entry) is None

    assert hasher.hashed_bytes == 0
    assert cache.hashes == {}


def test_returns_none_if_file_changed_between_stats(tmp_path: Path) -> None:
    """Reject and avoid caching a digest if the file changes after data has been read."""
    path = tmp_path / "file"
    content = b"data"
    path.write_bytes(content)
    entry = _entry(path, tmp_path)
    cache = _Cache()
    checks = 0

    def change_mtime() -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            mtime_ns = entry.mtime_ns + 1_000_000_000
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return False

    hasher = LocalHasher(tmp_path, cache, should_stop=change_mtime)

    assert hasher.md5(entry) is None
    assert checks == 2
    assert hasher.hashed_bytes == len(content)
    assert cache.hashes == {}


def test_returns_none_if_path_is_replaced_with_same_metadata(tmp_path: Path) -> None:
    """Reject a different inode replacing the path while the old file is open."""
    path = tmp_path / "file"
    path.write_bytes(b"a")
    entry = _entry(path, tmp_path)
    cache = _Cache()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"b")
    os.utime(replacement, ns=(entry.mtime_ns, entry.mtime_ns))

    def replace_path() -> bool:
        if replacement.exists():
            replacement.replace(path)
        return False

    hasher = LocalHasher(tmp_path, cache, should_stop=replace_path)

    assert hasher.md5(entry) is None
    assert _entry(path, tmp_path) == entry
    assert hasher.hashed_bytes == 1
    assert cache.hashes == {}


def test_hash_checks_identity_of_opened_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to read a different inode substituted between stat and open."""
    path = tmp_path / "file"
    path.write_bytes(b"a")
    entry = _entry(path, tmp_path)
    cache = _Cache()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"b")
    os.utime(replacement, ns=(entry.mtime_ns, entry.mtime_ns))
    original_open = builtins.open

    def replace_before_open(file: Path, *args: Any, **kwargs: Any) -> BinaryIO:
        assert file == path
        replacement.replace(path)
        return original_open(file, *args, **kwargs)

    hasher = LocalHasher(tmp_path, cache)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", replace_before_open)
        assert hasher.md5(entry) is None

    assert hasher.hashed_bytes == 0
    assert cache.hashes == {}


@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_returns_none_on_missing_or_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Return None on a vanished file or a permission error during open."""
    path = tmp_path / "file"
    path.write_bytes(b"data")
    entry = _entry(path, tmp_path)
    cache = _Cache()
    hasher = LocalHasher(tmp_path, cache)

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("cannot open file")

    if failure == "missing":
        path.unlink()
    with monkeypatch.context() as patch:
        if failure == "unreadable":
            patch.setattr(builtins, "open", denied)
            patch.setattr(io, "open", denied)
            patch.setattr(os, "open", denied)
        assert hasher.md5(entry) is None

    assert hasher.hashed_bytes == 0
    assert cache.hashes == {}


@pytest.mark.parametrize("rel", ["Docs", "Docs/nested", "Docs/nested/file"])
def test_hasher_does_not_follow_substituted_symlinks(tmp_path: Path, rel: str) -> None:
    """Reject a file or ancestor substituted by a symlink since the scan."""
    path = tmp_path / "Docs" / "nested" / "file"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"data")
    entry = _entry(path, tmp_path)
    replaced = tmp_path / rel
    target = replaced.with_name("original")
    replaced.rename(target)
    replaced.symlink_to(target, target_is_directory=target.is_dir())
    hasher = LocalHasher(tmp_path, _Cache())

    assert hasher.md5(entry) is None
    assert hasher.hashed_bytes == 0


def test_hasher_does_not_hash_directories(tmp_path: Path) -> None:
    """Return None for a directory entry without consulting its cache."""
    folder = tmp_path / "Docs"
    folder.mkdir()
    entry = LocalEntry("Docs", True, 0, folder.stat().st_mtime_ns)
    cache = _Cache()
    cache.put_hash(entry.rel, entry.size, entry.mtime_ns, _digest(b""))

    assert LocalHasher(tmp_path, cache).md5(entry) is None


def test_hasher_reads_disk_rel_for_nfd_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open the supplied NFD path directly and cache the digest by its NFC key."""
    path = tmp_path / "Cafe\u0301" / "re\u0301sume\u0301.txt"
    path.parent.mkdir()
    path.write_bytes(b"data")
    entry = _entry(path, tmp_path, rel="Caf\u00e9/r\u00e9sum\u00e9.txt")
    cache = _Cache()

    def deny_listing(*args: object, **kwargs: object) -> None:
        raise AssertionError("the disk path is already known")

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", deny_listing)
        assert LocalHasher(tmp_path, cache).md5(entry) == _digest(b"data")

    assert cache.hashes == {(entry.rel, entry.size, entry.mtime_ns): _digest(b"data")}


def test_hashes_disk_winner_when_both_spellings_exist(tmp_path: Path) -> None:
    """Read the scanner's raw-name winner even if both files have matching metadata."""
    top = tmp_path / "Docs"
    decomposed_dir = top / "Cafe\u0301"
    decomposed_dir.mkdir(parents=True)
    composed = decomposed_dir / "\u00e9.txt"
    decomposed = decomposed_dir / "e\u0301.txt"
    composed.write_bytes(b"b")
    decomposed.write_bytes(b"a")
    mtime_ns = 1_700_000_000_123_456_789
    os.utime(composed, ns=(mtime_ns, mtime_ns))
    os.utime(decomposed, ns=(mtime_ns, mtime_ns))
    entry = _entry(decomposed, tmp_path, rel="Docs/Caf\u00e9/\u00e9.txt")

    assert LocalHasher(tmp_path, _Cache()).md5(entry) == _digest(b"a")


def test_keeps_nfc_path_when_nfd_sibling_appears(tmp_path: Path) -> None:
    """Read the recorded NFC spelling even if a decomposed sibling appears after the scan."""
    composed = tmp_path / "\u00e9.txt"
    composed.write_bytes(b"a")
    entry = _entry(composed, tmp_path)

    decomposed = tmp_path / "e\u0301.txt"
    decomposed.write_bytes(b"b")
    os.utime(decomposed, ns=(entry.mtime_ns, entry.mtime_ns))

    assert LocalHasher(tmp_path, _Cache()).md5(entry) == _digest(b"a")


def test_hashes_ascii_nfc_alias(tmp_path: Path) -> None:
    """Find a non-ASCII disk name that normalizes to an ASCII component."""
    path = tmp_path / "\u212a.txt"
    path.write_bytes(b"data")
    entry = _entry(path, tmp_path, rel="K.txt")

    assert LocalHasher(tmp_path, _Cache()).md5(entry) == _digest(b"data")


def test_hashed_bytes_counter(tmp_path: Path) -> None:
    """Count physical content reads, excluding cache hits and empty files."""
    hasher = LocalHasher(tmp_path, _Cache())
    path = tmp_path / "file"
    complete = (4 << 20) + 3
    path.write_bytes(b"x" * complete)
    entry = _entry(path, tmp_path)
    empty_path = tmp_path / "empty"
    empty_path.touch()

    assert hasher.hashed_bytes == 0
    assert hasher.md5(entry) is not None
    assert hasher.hashed_bytes == complete
    assert hasher.md5(entry) is not None
    assert hasher.md5(_entry(empty_path, tmp_path)) == _digest(b"")
    assert hasher.hashed_bytes == complete
    assert hasher.md5(entry, force=True) is not None
    assert hasher.hashed_bytes == 2 * complete


def test_should_stop_during_hash(tmp_path: Path) -> None:
    """Propagate Stopped between chunks and count only the bytes already read."""
    path = tmp_path / "file"
    chunk_size = 4 << 20
    path.write_bytes(b"a" * (chunk_size + 3))
    cache = _Cache()
    checks = 0

    def should_stop() -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    hasher = LocalHasher(tmp_path, cache, should_stop=should_stop)

    with pytest.raises(Stopped):
        hasher.md5(_entry(path, tmp_path))

    assert checks == 2
    assert hasher.hashed_bytes == chunk_size
    assert cache.hashes == {}

    checks = 0
    with pytest.raises(Stopped):
        md5_file(path, chunk_size=2, should_stop=should_stop)
    assert checks == 2


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_md5_file_rejects_nonpositive_chunk_size(tmp_path: Path, chunk_size: int) -> None:
    """Require a positive chunk size to preserve streaming and cancellation."""
    path = tmp_path / "file"
    path.write_bytes(b"data")

    with pytest.raises(ValueError, match="chunk_size"):
        md5_file(path, chunk_size=chunk_size)


def test_hasher_rejects_descriptor_outside_source_after_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    branch = root / "branch"
    branch.mkdir(parents=True)
    path = branch / "file"
    path.write_bytes(b"data")
    entry = _entry(path, root)
    outside = tmp_path / "outside"
    original = os.open

    def swap_before_open(candidate: str, flags: int) -> int:
        branch.rename(outside)
        branch.symlink_to(outside, target_is_directory=True)
        return original(candidate, flags)

    monkeypatch.setattr(os, "open", swap_before_open)
    hasher = LocalHasher(root, _Cache())
    assert hasher.md5(entry) is None
    assert hasher.hashed_bytes == 0
