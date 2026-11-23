"""Retry-safe Drive uploads and verified local restores."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from .drive import DriveClient
from .errors import Conflict, FileChanged, Md5Mismatch, SessionExpired, Stopped
from .model import PART_SUFFIX, RemoteFile, drive_ms

_BLOCK = 256 << 10


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Describe a scanned file and its generated create ID or existing update ID."""

    rel: str
    path: Path
    size: int
    mtime_ns: int
    file_id: str
    update: bool
    name: str | None = None
    parent_id: str | None = None
    properties: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """Return the verified file, its full local digest, and media sent in this call.

    Media handed to the HTTP transport is counted on each attempt. This excludes
    HTTP framing, metadata, status probes and bytes confirmed before the call.
    """

    file: RemoteFile
    local_md5: str
    bytes_sent: int


class SessionStore(Protocol):
    """Durably look up and modify upload capabilities by exact local file version."""

    def get_session(self, rel: str, size: int, mtime_ns: int, kind: str) -> tuple[str, str] | None:
        """Return the saved file ID and URL, or None for a missing key."""

    def put_session(
        self, rel: str, size: int, mtime_ns: int, kind: str, file_id: str, url: str
    ) -> None:
        """Commit an upload capability before sending its first data chunk."""

    def delete_session(self, rel: str, size: int, mtime_ns: int, kind: str) -> None:
        """Remove the saved capability for this exact key, if present."""


def _check_stat(req: UploadRequest, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (
        req.size,
        req.mtime_ns,
    ):
        raise FileChanged(req.rel)


def _open_source(req: UploadRequest) -> BinaryIO:
    try:
        fd = os.open(req.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileChanged(req.rel) from exc
        raise
    return os.fdopen(fd, "rb")


def _check_source(req: UploadRequest, source: BinaryIO, path_stat: os.stat_result) -> None:
    handle_stat = os.fstat(source.fileno())
    _check_stat(req, path_stat)
    _check_stat(req, handle_stat)
    if not os.path.samestat(path_stat, handle_stat):
        raise FileChanged(req.rel)


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise Stopped()


class _UploadData:
    def __init__(
        self, req: UploadRequest, source: BinaryIO, on_bytes: Callable[[int], None] | None
    ) -> None:
        self.req = req
        self.source = source
        self._on_bytes = on_bytes
        self._digest = hashlib.md5(usedforsecurity=False)
        self.offset = 0
        self._reported = 0
        self.sent = 0

    @property
    def md5(self) -> str:
        return self._digest.hexdigest()

    def read(self, offset: int, length: int) -> bytes:
        self.source.seek(offset)
        chunk = self.source.read(length)
        if len(chunk) != length:
            raise FileChanged(self.req.rel)
        return chunk

    def count_sent(self, count: int) -> None:
        self.sent += count

    def reset(self) -> None:
        self._digest = hashlib.md5(usedforsecurity=False)
        self.offset = 0

    def confirm(self, offset: int, chunk: bytes = b"") -> None:
        if offset < self.offset:
            self.reset()
            chunk = b""
        accepted = chunk[: offset - self.offset]
        self._digest.update(accepted)
        self.offset += len(accepted)
        while self.offset < offset:
            block = self.read(self.offset, min(_BLOCK, offset - self.offset))
            self._digest.update(block)
            self.offset += len(block)

    def report(self) -> None:
        if self.offset > self._reported:
            delta = self.offset - self._reported
            self._reported = self.offset
            if self._on_bytes is not None:
                self._on_bytes(delta)

    def matches(self, item: RemoteFile) -> bool:
        return (item.id, item.size, item.md5) == (self.req.file_id, self.req.size, self.md5)


class Uploader:
    """Use multipart for small files and persist resumable sessions for large ones."""

    def __init__(
        self,
        drive: DriveClient,
        sessions: SessionStore,
        *,
        chunk_size: int = 32 << 20,
        multipart_max: int = 8 << 20,
    ) -> None:
        """Set the chunk size and inclusive multipart limit in bytes.

        A negative multipart limit forces resumable uploads. Raise ValueError
        unless chunks are a positive multiple of 256 KiB.
        """
        if chunk_size <= 0 or chunk_size % _BLOCK:
            raise ValueError("Chunk size must be a positive multiple of 262144")
        self._drive = drive
        self._sessions = sessions
        self._chunk_size = chunk_size
        self._multipart_max = multipart_max

    def upload(
        self,
        req: UploadRequest,
        *,
        on_bytes: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> UploadOutcome:
        """Upload a scanned file, reporting newly confirmed bytes as positive deltas.

        Stopped leaves any pending session usable. FileChanged discards a stale
        session. Md5Mismatch indicates unverified remote content. Local I/O and
        typed Drive errors propagate to the caller.
        """
        key = (req.rel, req.size, req.mtime_ns, "update" if req.update else "create")
        try:
            before = req.path.lstat()
            _check_stat(req, before)
            _check_stop(should_stop)
            with _open_source(req) as source:
                _check_source(req, source, before)
                data = _UploadData(req, source, on_bytes)
                if req.size <= self._multipart_max:
                    item = self._multipart(data)
                else:
                    item = self._resumable(data, key, should_stop)
                self._check_path(data)
                if not data.matches(item):
                    raise Md5Mismatch(req.rel)
                return UploadOutcome(item, data.md5, data.sent)
        except FileChanged:
            saved = self._sessions.get_session(*key)
            if saved is not None and saved[0] == req.file_id:
                self._sessions.delete_session(*key)
            raise

    @staticmethod
    def _check_path(data: _UploadData) -> None:
        req = data.req
        try:
            path_stat = req.path.lstat()
        except FileNotFoundError as exc:
            raise FileChanged(req.rel) from exc
        _check_source(req, data.source, path_stat)

    def _multipart(self, data: _UploadData) -> RemoteFile:
        req = data.req
        content = data.read(0, req.size)
        data.confirm(req.size, content)
        self._check_path(data)
        try:
            item = self._drive._multipart(
                data=content,
                file_id=req.file_id,
                name=req.name,
                parent_id=req.parent_id,
                modified_ms=drive_ms(req.mtime_ns),
                properties=req.properties,
                update=req.update,
                on_send=data.count_sent,
            )
        except Conflict:
            completed = self._completed_create(data)
            if completed is None:
                raise
            item = completed
        data.report()
        return item

    def _completed_create(self, data: _UploadData) -> RemoteFile | None:
        if data.req.update:
            return None
        item = self._drive.get(data.req.file_id)
        if item is not None and item.size == data.req.size:
            data.confirm(data.req.size)
            if data.matches(item):
                return item
        return None

    def _completed_update(self, data: _UploadData) -> RemoteFile | None:
        req = data.req
        if not req.update:
            return None
        item = self._drive.get(req.file_id)
        if (
            item is not None
            and item.size == req.size
            and item.modified_ms == drive_ms(req.mtime_ns)
            and all(item.properties.get(key) == value for key, value in req.properties.items())
        ):
            data.confirm(req.size)
            if data.matches(item):
                return item
            data.reset()
        return None

    def _resumable(
        self,
        data: _UploadData,
        key: tuple[str, int, int, str],
        should_stop: Callable[[], bool] | None,
    ) -> RemoteFile:
        req = data.req
        saved = self._sessions.get_session(*key)
        if saved is not None and saved[0] != req.file_id:
            self._sessions.delete_session(*key)
            saved = None
        restarted = False
        while True:
            try:
                if saved is None:
                    url = self._drive.start_resumable(
                        size=req.size,
                        file_id=req.file_id,
                        name=req.name,
                        parent_id=req.parent_id,
                        modified_ms=drive_ms(req.mtime_ns),
                        properties=req.properties,
                        update=req.update,
                    )
                    self._sessions.put_session(*key, req.file_id, url)
                    position: int | RemoteFile = 0
                else:
                    url = saved[1]
                    position = self._drive.resumable_status(url, req.size)
                item = self._send_chunks(data, url, position, should_stop)
            except SessionExpired:
                self._sessions.delete_session(*key)
                if restarted:
                    raise
                _check_stop(should_stop)
                if (completed := self._completed_update(data)) is not None:
                    item = completed
                    break
                data.reset()
                saved = None
                restarted = True
            except Conflict:
                self._sessions.delete_session(*key)
                completed = self._completed_create(data)
                if completed is None:
                    raise
                item = completed
                break
            else:
                self._sessions.delete_session(*key)
                break

        data.report()
        return item

    def _send_chunks(
        self,
        data: _UploadData,
        url: str,
        position: int | RemoteFile,
        should_stop: Callable[[], bool] | None,
    ) -> RemoteFile:
        size = data.req.size
        data.confirm(size if isinstance(position, RemoteFile) else position)
        while isinstance(position, int):
            data.report()
            _check_stop(should_stop)
            offset = position
            chunk = data.read(offset, min(self._chunk_size, size - offset))

            def recover(previous: int = offset) -> int | RemoteFile | None:
                _check_stop(should_stop)
                status = self._drive.resumable_status(url, size)
                return status if status != previous else None

            position = self._drive._put_chunk(
                url, chunk, offset, size, on_send=data.count_sent, recover=recover
            )
            data.confirm(size if isinstance(position, RemoteFile) else position, chunk)
        return position


class Downloader:
    """Verify downloads before publishing them at an unused destination."""

    def __init__(self, drive: DriveClient) -> None:
        """Use the given authenticated client to fetch media."""
        self._drive = drive

    def download(
        self, file: RemoteFile, dest: Path, *, on_bytes: Callable[[int], None] | None = None
    ) -> None:
        """Restore a file, preserving mtime and reporting streamed byte deltas.

        Raise FileExistsError if the destination is occupied, including by a
        concurrent writer, and Md5Mismatch for a size or checksum mismatch.
        Clean up the owned staging file on any exit.
        """
        if os.path.lexists(dest):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + PART_SUFFIX)
        sink = part.open("x+b")
        try:
            with sink:
                self._drive.download(file.id, sink, on_bytes=on_bytes)
                size = sink.tell()
                sink.seek(0)
                digest = hashlib.file_digest(
                    sink, lambda: hashlib.md5(usedforsecurity=False)
                ).hexdigest()
                if (size, digest) != (file.size, file.md5):
                    raise Md5Mismatch(file.name)
            if file.modified_ms is not None:
                ns = file.modified_ms * 1_000_000
                os.utime(part, ns=(ns, ns))
            # Publish atomically without allowing replacement of an existing name.
            os.link(part, dest)
        finally:
            part.unlink(missing_ok=True)
