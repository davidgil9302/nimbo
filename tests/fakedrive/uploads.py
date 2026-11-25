"""Multipart parsing and resumable upload sessions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from email import policy
from email.parser import BytesParser
from typing import Any
from uuid import uuid4

import httpx

from . import fields
from .state import BINARY_MIME, DriveAPIError, DriveState, FakeFile

UPLOAD_PATH = "/upload/drive/v3/files"
_CHUNK_SIZE = 256 << 10
_CONTENT_RANGE = re.compile(r"bytes (?:(?P<start>[0-9]+)-(?P<end>[0-9]+)|\*)/(?P<total>[0-9]+)")


def decimal(value: str) -> int:
    """Read an unsigned ASCII decimal, raising ValueError on malformed input."""
    if not value.isascii() or not value.isdecimal():
        raise ValueError("Expected an unsigned decimal")
    return int(value)


def metadata_json(data: bytes) -> dict[str, Any]:
    """Decode a JSON metadata object or raise a Drive badRequest."""
    try:
        result = json.loads(data.decode("utf-8"))
        # Reject lone Unicode surrogates before a mutation can reach response encoding.
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as err:
        raise DriveAPIError.bad_request("Invalid JSON metadata.") from err
    if not isinstance(result, dict):
        raise DriveAPIError.bad_request("File metadata must be a JSON object.")
    return result


def metadata_request(request: httpx.Request, *, optional: bool = False) -> dict[str, Any]:
    """Read JSON metadata, with an optional empty body for resumable initiation."""
    if optional and not request.content:
        return {}
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise DriveAPIError.bad_request("Expected application/json metadata.")
    return metadata_json(request.content)


def _multipart(request: httpx.Request) -> tuple[dict[str, Any], bytes, str]:
    content_type = request.headers.get("Content-Type", "")
    if "\r" in content_type or "\n" in content_type:
        raise DriveAPIError.bad_request("Invalid multipart Content-Type.")
    raw_headers = b"Content-Type: " + content_type.encode("ascii", "replace") + b"\r\n\r\n"
    message = BytesParser(policy=policy.default).parsebytes(raw_headers + request.content)
    parts = list(message.iter_parts())
    if message.get_content_type() != "multipart/related" or len(parts) != 2 or message.defects:
        raise DriveAPIError.bad_request("Expected multipart/related with metadata and media parts.")
    metadata, media = parts
    if metadata.get_content_type() != "application/json" or media.get("Content-Type") is None:
        raise DriveAPIError.bad_request("Expected JSON metadata followed by typed media.")
    meta_bytes = metadata.get_payload(decode=True)
    media_bytes = media.get_payload(decode=True)
    if not isinstance(meta_bytes, bytes) or not isinstance(media_bytes, bytes):
        raise DriveAPIError.bad_request("Invalid multipart parts.")
    if metadata.defects or media.defects:
        raise DriveAPIError.bad_request("Invalid multipart encoding.")
    return metadata_json(meta_bytes), media_bytes, media.get_content_type()


@dataclass(slots=True)
class _Session:
    path: str
    file_id: str | None
    metadata: dict[str, Any]
    mask: fields.Mask
    media_type: str
    total: int | None
    add_parents: str | None
    remove_parents: str | None
    buffer: bytearray = field(default_factory=bytearray)
    result: dict[str, Any] | None = None


class Uploads:
    """Serve upload sessions and commit files only when their content is complete."""

    def __init__(self, state: DriveState) -> None:
        self._state = state
        self._sessions: dict[str, _Session] = {}

    def expire(self) -> None:
        """Invalidate all resumable session IDs, including completed ones."""
        self._sessions.clear()

    def initiate(
        self, request: httpx.Request, file_id: str | None, base: str, mask: fields.Mask
    ) -> httpx.Response:
        """Handle multipart upload or resumable session creation."""
        query = request.url.params
        upload_type = query.get("uploadType")
        if upload_type == "multipart":
            metadata, content, media_type = _multipart(request)
            session = _Session(
                request.url.path,
                file_id,
                metadata,
                mask,
                media_type,
                len(content),
                query.get("addParents"),
                query.get("removeParents"),
            )
            return httpx.Response(200, json=self._finish(session, content))
        if upload_type != "resumable":
            raise DriveAPIError.bad_request(
                "Expected uploadType=multipart or uploadType=resumable."
            )

        metadata = metadata_request(request, optional=True)
        try:
            length = request.headers.get("X-Upload-Content-Length")
            total = None if length is None else decimal(length)
        except ValueError as err:
            raise DriveAPIError.bad_request("Invalid X-Upload-Content-Length.") from err
        media_type = request.headers.get("X-Upload-Content-Type", BINARY_MIME)
        media_type = media_type.partition(";")[0].strip().lower()
        session = _Session(
            request.url.path,
            file_id,
            metadata,
            mask,
            media_type,
            total,
            query.get("addParents"),
            query.get("removeParents"),
        )
        item = self._prepare(session)
        self._state.check_upload(item, total if total is not None else item.size or 0)
        self._state.check_write()
        upload_id = uuid4().hex
        self._sessions[upload_id] = session
        self._state.record_write()
        path = request.url.raw_path.partition(b"?")[0].decode("ascii")
        return httpx.Response(200, headers={"Location": f"{base}{path}?upload_id={upload_id}"})

    def put(self, request: httpx.Request) -> httpx.Response:
        """Apply a chunk or return the progress of a resumable session."""
        upload_id = request.url.params.get("upload_id", "")
        session = self._sessions.get(upload_id)
        if session is None or session.path != request.url.path:
            raise DriveAPIError(404, "notFound", "Upload session not found.")
        match = _CONTENT_RANGE.fullmatch(request.headers.get("Content-Range", ""))
        if match is None:
            raise DriveAPIError.bad_request("Invalid Content-Range.")
        try:
            total = decimal(match["total"])
            start, end = (
                (None, None)
                if match["start"] is None
                else (decimal(match["start"]), decimal(match["end"]))
            )
        except ValueError as err:
            raise DriveAPIError.bad_request("Invalid Content-Range.") from err
        if session.total is not None and total != session.total:
            raise DriveAPIError.bad_request("Content-Range does not match the upload length.")
        chunk = request.content
        if start is None:
            if chunk:
                raise DriveAPIError.bad_request("Status queries require an empty body.")
            if session.result is not None or total > 0:
                return self._status(session)
            return self._complete(session, b"")
        assert end is not None
        if not start <= end < total or len(chunk) != end - start + 1:
            raise DriveAPIError.bad_request("Content-Range does not match the request body.")
        if session.result is not None or start != len(session.buffer):
            return self._status(session)
        if end + 1 == total:
            content = b"".join((session.buffer, chunk)) if session.buffer else chunk
            return self._complete(session, content)
        if len(chunk) % _CHUNK_SIZE:
            raise DriveAPIError.bad_request("Non-final chunks must be multiples of 262144 bytes.")
        if session.total is None:
            self._state.check_upload(self._prepare(session), total)
        self._state.check_write()
        session.total = total
        session.buffer.extend(chunk)
        self._state.record_write()
        return self._status(session)

    def _prepare(self, session: _Session) -> FakeFile:
        return self._state.prepare(
            session.metadata,
            session.file_id,
            media_type=session.media_type,
            add_parents=session.add_parents,
            remove_parents=session.remove_parents,
        )

    def _finish(self, session: _Session, content: bytes) -> dict[str, Any]:
        item = self._prepare(session)
        self._state.check_upload(item, len(content))
        self._state.check_write()
        if "modifiedTime" not in session.metadata:
            item = replace(item, modified_ms=self._state.millis())
        item = self._state.commit(item, content=content)
        self._state.record_upload(len(content))
        self._state.record_write()
        return fields.select(item.as_api(), session.mask)

    def _complete(self, session: _Session, content: bytes) -> httpx.Response:
        session.result = self._finish(session, content)
        session.total = len(content)
        session.buffer.clear()
        return self._status(session)

    @staticmethod
    def _status(session: _Session) -> httpx.Response:
        if session.result is not None:
            return httpx.Response(200, json=session.result)
        headers = {"Range": f"bytes=0-{len(session.buffer) - 1}"} if session.buffer else {}
        return httpx.Response(308, headers=headers)
