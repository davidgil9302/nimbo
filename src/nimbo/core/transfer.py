"""Retry-safe Drive uploads and verified local restores."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO, Protocol

from .drive import DriveClient
from .errors import Conflict, FileChanged, Md5Mismatch, RemoteChanged, SessionExpired, Stopped
from .hasher import check_source_scope
from .model import PART_SUFFIX, RemoteFile, drive_ms
from .tree import _rel_component

_BLOCK = 256 << 10


def create_session_kind(parent_id: str) -> str:
    """Return the session key component for a create under this Drive parent."""
    return f"create:{parent_id}"


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Describe a scanned file, its canonical source root and its Drive destination."""

    rel: str
    path: Path
    size: int
    mtime_ns: int
    file_id: str
    update: bool
    name: str | None = None
    parent_id: str | None = None
    properties: Mapping[str, str] = field(default_factory=dict)
    source_root: Path | None = None
    destination_rel: str | None = None
    expected_remote: RemoteFile | None = None


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


def _session_kind(req: UploadRequest) -> str:
    if req.update:
        return "update"
    if not req.parent_id:
        raise ValueError("A create upload requires a parent ID")
    return create_session_kind(req.parent_id)


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
        self,
        req: UploadRequest,
        source: BinaryIO,
        on_bytes: Callable[[int], None] | None,
        root_id: str | None,
        check_create_parent: Callable[[str, str | None], bool] | None,
    ) -> None:
        self.req = req
        self.source = source
        self._on_bytes = on_bytes
        self.root_id = root_id
        self.check_create_parent = check_create_parent
        self._digest = hashlib.md5(usedforsecurity=False)
        self.offset = 0
        self._reported = 0
        self.sent = 0

    @property
    def md5(self) -> str:
        return self._digest.hexdigest()

    def read(self, offset: int, length: int) -> bytes:
        if self.req.source_root is not None:
            check_source_scope(self.source, self.req.source_root, self.req.rel)
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
        return (
            not item.trashed
            and not item.is_folder
            and not item.is_google_native
            and (item.id, item.size, item.md5) == (self.req.file_id, self.req.size, self.md5)
        )


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
        root_id: str | None = None,
        check_create_parent: Callable[[str, str | None], bool] | None = None,
    ) -> UploadOutcome:
        """Upload a scanned file, reporting newly confirmed bytes as positive deltas.

        Retry a size or checksum mismatch once as an update on the same ID.
        An occupied create ID requires matching live identity and verified content.
        Stopped leaves any pending session usable. FileChanged discards a stale
        session. When a root is supplied, RemoteChanged aborts
        transfers that lose live membership. Md5Mismatch indicates unverified
        remote content. Local I/O and typed Drive errors propagate.

        A caller that revalidates names against a refreshed Drive index may
        supply a parent checker for the first send of a new multipart create.
        Retries, resumable transfers and updates use the regular live checks.
        """
        try:
            before = req.path.lstat()
            _check_stat(req, before)
            _check_stop(should_stop)
            with _open_source(req) as source:
                if req.source_root is not None:
                    check_source_scope(source, req.source_root, req.rel)
                _check_source(req, source, before)
                data = _UploadData(req, source, on_bytes, root_id, check_create_parent)
                item = self._upload_verified(data, should_stop)
                return UploadOutcome(item, data.md5, data.sent)
        except FileChanged:
            if not req.update:
                self._discard_session(req, _session_kind(req))
            self._discard_session(req, "update")
            raise

    def _discard_session(self, req: UploadRequest, kind: str) -> None:
        key = (req.rel, req.size, req.mtime_ns, kind)
        saved = self._sessions.get_session(*key)
        if saved is not None and saved[0] == req.file_id:
            self._sessions.delete_session(*key)

    def _upload_verified(
        self, data: _UploadData, should_stop: Callable[[], bool] | None
    ) -> RemoteFile:
        req = data.req
        resume_repair = False
        try:
            item = self._upload_attempt(data, should_stop)
        except Conflict:
            if req.update or (existing := self._create_target(req)) is None:
                raise
            data.confirm(req.size)
            if data.matches(existing):
                self._check_mirror_scope(data)
                data.report()
                self._check_path(data)
                # A prior repair may have committed before its session could be cleared.
                self._discard_session(req, "update")
                return existing
            saved = self._sessions.get_session(req.rel, req.size, req.mtime_ns, "update")
            if saved is None or saved[0] != req.file_id:
                raise
            resume_repair = True
        else:
            if not req.update and not self._matches_create(req, item):
                raise Conflict(req.rel)
            if data.matches(item):
                return item
            if not req.update and self._create_target(req) is None:
                raise Conflict(req.rel)

        _check_stop(should_stop)
        self._check_path(data)
        if not req.update:
            expected = existing if resume_repair else item
            data.req = replace(
                req, update=True, name=None, parent_id=None, expected_remote=expected
            )
        elif req.expected_remote is not None:
            data.req = replace(req, expected_remote=item)
        data.reset()
        item = self._upload_attempt(data, should_stop, resume_only=resume_repair)
        if not data.matches(item):
            raise Md5Mismatch(req.rel)
        return item

    def _upload_attempt(
        self,
        data: _UploadData,
        should_stop: Callable[[], bool] | None,
        *,
        resume_only: bool = False,
    ) -> RemoteFile:
        if data.req.size <= self._multipart_max and not resume_only:
            item = self._multipart(data, should_stop)
        else:
            item = self._resumable(data, should_stop, resume_only=resume_only)
        self._check_path(data)
        return item

    @staticmethod
    def _check_path(data: _UploadData) -> None:
        req = data.req
        if req.source_root is not None:
            check_source_scope(data.source, req.source_root, req.rel)
        try:
            path_stat = req.path.lstat()
        except FileNotFoundError as exc:
            raise FileChanged(req.rel) from exc
        _check_source(req, data.source, path_stat)

    def _multipart(self, data: _UploadData, should_stop: Callable[[], bool] | None) -> RemoteFile:
        req = data.req
        content = data.read(0, req.size)
        data.confirm(req.size, content)
        check_parent = (
            data.check_create_parent if data.root_id is not None and not req.update else None
        )

        def before_send() -> None:
            nonlocal check_parent
            if check_parent is None:
                self._before_write(data)
                return
            assert req.parent_id is not None
            parent_rel = (
                req.destination_rel.rpartition("/")[0] if req.destination_rel is not None else None
            )
            if not check_parent(req.parent_id, parent_rel):
                raise RemoteChanged(req.rel)
            self._check_path(data)
            # A failed send may have created this ID. Revalidate it live on retry.
            check_parent = None

        item = self._drive._multipart(
            data=content,
            file_id=req.file_id,
            name=req.name,
            parent_id=req.parent_id,
            modified_ms=drive_ms(req.mtime_ns),
            properties=req.properties,
            update=req.update,
            on_send=data.count_sent,
            before_send=before_send,
            should_stop=should_stop,
            validate_recovered=lambda item: self._check_recovered_upload(data, item),
        )
        data.report()
        return item

    def _create_target(self, req: UploadRequest) -> RemoteFile | None:
        item = self._drive.get(req.file_id)
        return item if item is not None and self._matches_create(req, item) else None

    def _matches_create(self, req: UploadRequest, item: RemoteFile) -> bool:
        if (
            item.id != req.file_id
            or item.trashed
            or item.is_folder
            or item.is_google_native
            or item.name != req.name
        ):
            return False
        if item.parents == (req.parent_id,):
            return True
        root = self._drive.get("root") if req.parent_id == "root" else None
        return root is not None and item.parents == (root.id,)

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
            self._check_mirror_scope(data)
            data.confirm(req.size)
            if data.matches(item):
                return item
            data.reset()
        return None

    def _check_mirror_scope(self, data: _UploadData) -> RemoteFile | None:
        root = data.root_id
        if root is None:
            return None
        req = data.req
        item = self._drive.get(req.file_id)
        parent = req.parent_id
        expected_parent_rel = None
        if req.destination_rel is not None:
            expected_parent_rel, _, expected_name = req.destination_rel.rpartition("/")
            if req.update and item is not None and _rel_component(item.name) != expected_name:
                raise RemoteChanged(req.rel)
        if req.update:
            parent = item.parents[0] if item is not None and len(item.parents) == 1 else None
        if item is not None and (
            item.trashed
            or item.is_folder
            or item.is_google_native
            or (not req.update and not self._matches_create(req, item))
        ):
            raise RemoteChanged(req.rel)
        if parent is None or not self._drive.live_folder_in_subtree(
            parent, root, rel=expected_parent_rel
        ):
            raise RemoteChanged(req.rel)
        return item

    def _before_write(self, data: _UploadData) -> None:
        req = data.req
        live = self._check_mirror_scope(data)
        if (
            data.root_id is not None
            and req.expected_remote is not None
            and live != req.expected_remote
        ):
            raise RemoteChanged(req.rel)
        if data.root_id is not None and not req.update:
            assert req.parent_id is not None and req.name is not None
            if any(
                child.id != req.file_id
                for child in self._drive.list_children([req.parent_id], name=req.name)
            ):
                raise RemoteChanged(req.rel)
        self._check_path(data)

    def _resumable(
        self,
        data: _UploadData,
        should_stop: Callable[[], bool] | None,
        *,
        resume_only: bool,
    ) -> RemoteFile:
        req = data.req
        key = (req.rel, req.size, req.mtime_ns, _session_kind(req))
        saved = self._sessions.get_session(*key)
        if saved is not None and saved[0] != req.file_id:
            self._sessions.delete_session(*key)
            saved = None
        restarted = False
        while True:
            try:
                if saved is None:
                    if resume_only:
                        raise SessionExpired(req.rel)
                    url = self._drive.start_resumable(
                        size=req.size,
                        file_id=req.file_id,
                        name=req.name,
                        parent_id=req.parent_id,
                        modified_ms=drive_ms(req.mtime_ns),
                        properties=req.properties,
                        update=req.update,
                        before_send=lambda: self._before_write(data),
                        should_stop=should_stop,
                    )
                    self._sessions.put_session(*key, req.file_id, url)
                    position: int | RemoteFile = 0
                else:
                    url = saved[1]
                    self._check_mirror_scope(data)
                    position = self._drive.resumable_status(url, req.size, should_stop=should_stop)
                    if isinstance(position, RemoteFile):
                        position = self._check_completed_session(data, position)
                item = self._send_chunks(data, url, position, should_stop)
            except SessionExpired:
                self._sessions.delete_session(*key)
                if restarted or resume_only:
                    raise
                _check_stop(should_stop)
                if (completed := self._completed_update(data)) is not None:
                    item = completed
                    break
                data.reset()
                saved = None
                restarted = True
            except (Conflict, RemoteChanged):
                self._sessions.delete_session(*key)
                raise
            else:
                self._sessions.delete_session(*key)
                break

        data.report()
        return item

    def _check_completed_session(self, data: _UploadData, recorded: RemoteFile) -> RemoteFile:
        req = data.req
        live = (
            self._check_mirror_scope(data)
            if data.root_id is not None
            else self._drive.get(req.file_id)
        )
        if live is None or live != recorded or live.trashed:
            raise RemoteChanged(req.rel)
        return live

    def _check_recovered_upload(self, data: _UploadData, item: RemoteFile) -> None:
        if data.root_id is not None and self._check_mirror_scope(data) != item:
            raise RemoteChanged(data.req.rel)

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
            final = offset + len(chunk) == size

            def before_send(final_chunk: bool = final) -> None:
                if final_chunk:
                    self._before_write(data)
                else:
                    self._check_path(data)

            def recover(
                previous: int = offset, final_chunk: bool = final
            ) -> int | RemoteFile | None:
                _check_stop(should_stop)
                if final_chunk:
                    self._check_mirror_scope(data)
                status = self._drive.resumable_status(url, size, should_stop=should_stop)
                if isinstance(status, RemoteFile):
                    return self._check_completed_session(data, status)
                return status if status != previous else None

            position = self._drive._put_chunk(
                url,
                chunk,
                offset,
                size,
                on_send=data.count_sent,
                recover=recover,
                before_send=before_send,
                should_stop=should_stop,
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
