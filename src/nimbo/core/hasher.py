"""Stream file digests and cache them against observed filesystem metadata."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from nimbo.core.errors import FileChanged, Stopped
from nimbo.core.model import FileIdentity, LocalEntry

_CHUNK_SIZE = 4 << 20


class HashCache(Protocol):
    """Cache a digest by path, size, nanosecond mtime and local file identity."""

    def get_hash(
        self, rel: str, size: int, mtime_ns: int, *, identity: FileIdentity | None = None
    ) -> str | None:
        """Return the cached digest for matching metadata, or None on a miss."""
        ...

    def put_hash(
        self, rel: str, size: int, mtime_ns: int, md5: str, *, identity: FileIdentity | None = None
    ) -> None:
        """Store the digest for the given path and metadata."""
        ...


def _md5_stream(
    handle: BinaryIO,
    chunk_size: int,
    should_stop: Callable[[], bool] | None,
    on_read: Callable[[int], None] | None = None,
) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    while True:
        if should_stop is not None and should_stop():
            raise Stopped
        chunk = handle.read(chunk_size)
        if not chunk:
            return digest.hexdigest()
        if on_read is not None:
            on_read(len(chunk))
        digest.update(chunk)


def md5_file(
    path: Path,
    *,
    chunk_size: int = _CHUNK_SIZE,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Return the file's hex MD5, reading at most ``chunk_size`` bytes per chunk.

    Raise ``ValueError`` for a nonpositive chunk size, ``Stopped`` on request,
    and ``OSError`` on filesystem errors.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    with path.open("rb", buffering=0) as handle:
        return _md5_stream(handle, chunk_size, should_stop)


def _open_regular_candidate(path: str, flags: int) -> int:
    # A replacement symlink must fail and a replacement FIFO must not block the open.
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def _matches_entry(info: os.stat_result, entry: LocalEntry) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and (info.st_size, info.st_mtime_ns) == (entry.size, entry.mtime_ns)
        and (entry.identity is None or entry.identity == (info.st_ino, info.st_ctime_ns))
    )


def check_source_path(handle: BinaryIO, expected: Path, rel: str) -> None:
    """Raise FileChanged unless the descriptor names the expected physical path."""
    try:
        opened = os.readlink(f"/proc/self/fd/{handle.fileno()}")  # noqa: PTH115
    except OSError as exc:
        raise FileChanged(rel) from exc
    if opened != os.fspath(expected):
        raise FileChanged(rel)


class LocalHasher:
    """Hash regular files beneath a source root with a metadata cache.

    ``hashed_bytes`` counts bytes read from files, including reads discarded
    after a change or a stop request.
    """

    def __init__(
        self,
        root: Path,
        index: HashCache,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Use ``index`` for digests and check ``should_stop`` between reads."""
        self._root = root
        self._real_root = root.resolve()
        self._index = index
        self._should_stop = should_stop
        self.hashed_bytes: int = 0

    def _count_read(self, count: int) -> None:
        self.hashed_bytes += count

    def md5(self, entry: LocalEntry, *, force: bool = False) -> str | None:
        """Return the digest for ``entry``, bypassing cached data when ``force`` is set.

        Open the recorded disk path and key the cache by the NFC relative path.
        Return None on a directory, changed metadata or an OS error. Raise
        ``Stopped`` if cancellation is requested while reading a file.
        """
        if entry.is_dir:
            return None
        key = (entry.rel, entry.size, entry.mtime_ns)
        try:
            if not force:
                cached = self._index.get_hash(*key, identity=entry.identity)
                if cached is not None:
                    return cached

            disk_rel = Path(entry.disk_rel)
            if disk_rel.is_absolute() or ".." in disk_rel.parts:
                return None
            path = self._root / disk_rel
            expected = self._real_root / disk_rel
            # The last relative parent is '.', which represents the configured root.
            for parent in reversed(disk_rel.parents[:-1]):
                if not stat.S_ISDIR((self._root / parent).lstat().st_mode):
                    return None
            before = path.lstat()
            if not _matches_entry(before, entry):
                return None
            with open(path, "rb", buffering=0, opener=_open_regular_candidate) as handle:
                check_source_path(handle, expected, entry.rel)
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(before, opened) or not _matches_entry(opened, entry):
                    return None
                digest = _md5_stream(handle, _CHUNK_SIZE, self._should_stop, self._count_read)
                check_source_path(handle, expected, entry.rel)
                after = path.lstat()
                if not os.path.samestat(opened, after) or not _matches_entry(after, entry):
                    return None
            self._index.put_hash(*key, digest, identity=entry.identity)
        except (FileChanged, OSError):
            return None
        return digest
