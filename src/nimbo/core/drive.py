"""Authenticated Drive v3 requests with bounded retries and explicit field masks."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, BinaryIO, Literal, NoReturn, Protocol, TypeVar
from urllib.parse import quote
from uuid import uuid4

import httpx

from .errors import (
    BadRequest,
    Conflict,
    DriveError,
    Forbidden,
    NotFound,
    RateLimited,
    ServerError,
    SessionExpired,
    Stopped,
    StorageQuotaExceeded,
    TransportFailed,
    Unauthorized,
    UploadLimitExceeded,
)
from .model import FILE_FIELDS, FOLDER_MIME, About, Change, ChangesPage, RemoteFile, rfc3339_from_ms
from .ratelimit import RateLimiter, RetryPolicy, parse_retry_after
from .tree import _rel_component

DEFAULT_API_BASE = "https://www.googleapis.com"
LIST_GROUP_MAX = 40

_FILES = "/drive/v3/files"
_CHANGES = "/drive/v3/changes"
_UPLOAD = "/upload/drive/v3/files"
_BINARY = "application/octet-stream"
_CHANGE_FIELDS = f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
_QUOTA_REASONS = {"storageQuotaExceeded", "quotaExceeded"}
_T = TypeVar("_T")


class TokenProvider(Protocol):
    """Supply access tokens to the client and support invalidation after a 401."""

    def token(self) -> str:
        """Return a valid access token, refreshing it if necessary."""

    def invalidate(self) -> None:
        """Require the next token lookup to refresh the token."""


def _raise_for(response: httpx.Response, *, session: bool = False) -> NoReturn:
    status = response.status_code
    reason = ""
    message = response.reason_phrase
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(error := data.get("error"), dict):
        if isinstance(detail := error.get("message"), str):
            message = detail
        items = error.get("errors")
        if isinstance(items, list):
            reason = next(
                (
                    item["reason"]
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("reason"), str)
                ),
                "",
            )
    error_class: type[DriveError]
    match status:
        case 400:
            error_class = BadRequest
        case 401:
            error_class = Unauthorized
        case 403:
            if reason in _RATE_REASONS:
                error_class = RateLimited
            elif reason in _QUOTA_REASONS:
                error_class = StorageQuotaExceeded
            elif reason == "uploadLimitExceeded" or "upload limit" in message.lower():
                error_class = UploadLimitExceeded
            else:
                error_class = Forbidden
        case 404:
            error_class = SessionExpired if session else NotFound
        case 409:
            error_class = Conflict
        case 429:
            error_class = RateLimited
        case _:
            error_class = ServerError if 500 <= status < 600 else DriveError
    raise error_class(message, status=status, reason=reason)


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _media_metadata(
    file_id: str,
    name: str | None,
    parent_id: str | None,
    modified_ms: int,
    properties: Mapping[str, str] | None,
    update: bool,
) -> dict[str, object]:
    metadata: dict[str, object] = {"modifiedTime": rfc3339_from_ms(modified_ms)}
    if properties is not None:
        metadata["properties"] = dict(properties)
    if not update:
        if not file_id or name is None or parent_id is None:
            raise ValueError("An upload create requires a generated ID, name and parent")
        metadata.update(id=file_id, name=name, parents=[parent_id])
    return metadata


def _media_body(
    data: bytes,
    on_send: Callable[[int], None] | None,
    *,
    prefix: bytes = b"",
    suffix: bytes = b"",
) -> Iterator[bytes]:
    if prefix:
        yield prefix
    if data:
        if on_send is not None:
            on_send(len(data))
        yield data
    if suffix:
        yield suffix


class DriveClient:
    """Drive v3 client using a caller-owned HTTP connection pool and token provider.

    HTTP and transport failures raise typed DriveError subclasses. Each attempt
    acquires a token from the supplied read/write limiter.
    """

    def __init__(
        self,
        http: httpx.Client,
        tokens: TokenProvider,
        limiter: RateLimiter,
        retry: RetryPolicy,
        *,
        api_base: str = DEFAULT_API_BASE,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Set the API origin, retry policy, jitter source and sleep in seconds."""
        if retry.max_attempts < 1:
            raise ValueError("The retry policy must allow at least one attempt")
        self._http = http
        self._tokens = tokens
        self._limiter = limiter
        self._retry = retry
        self._api_base = httpx.URL(api_base.rstrip("/"))
        self._rng = random.SystemRandom() if rng is None else rng
        self._sleep = sleep

    def _request(
        self,
        method: str,
        url: str | httpx.URL,
        consume: Callable[[httpx.Response], _T],
        *,
        kind: Literal["read", "write"] = "read",
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | Callable[[], Iterable[bytes]] | None = None,
        json_body: Mapping[str, object] | None = None,
        session: bool = False,
        retry_http: bool = True,
        recover: Callable[[], _T | None] | None = None,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        single_attempt: bool = False,
    ) -> _T:
        if isinstance(url, str):
            url = self._api_base.join(url)
        if params is not None:
            url = url.copy_merge_params(params)
        refreshed = False
        attempt = 1
        while True:
            if should_stop is not None and should_stop():
                raise Stopped
            retry_after: float | None = None
            token = self._tokens.token()
            self._limiter.acquire(kind)
            request_headers = dict(headers or {})
            request_headers["Authorization"] = f"Bearer {token}"
            if before_send is not None:
                before_send()
            try:
                try:
                    request = self._http.build_request(
                        method,
                        url,
                        headers=request_headers,
                        content=content() if callable(content) else content,
                        json=json_body,
                    )
                    # Keep the chosen field mask and any opaque session parameters.
                    request.url = url
                    if should_stop is not None and should_stop():
                        raise Stopped
                    with closing(
                        self._http.send(request, stream=True, auth=None, follow_redirects=False)
                    ) as response:
                        if response.is_success or (session and response.status_code == 308):
                            return consume(response)
                        retry_after = parse_retry_after(
                            response.headers.get("Retry-After"), now=datetime.now(UTC)
                        )
                        response.read()
                        _raise_for(response, session=session)
                except httpx.TransportError as exc:
                    raise TransportFailed(type(exc).__name__) from exc
            except Unauthorized:
                if refreshed or single_attempt:
                    raise
                self._tokens.invalidate()
                refreshed = True
                continue
            except DriveError as exc:
                should_retry = (
                    exc.retryable
                    and not single_attempt
                    and attempt < self._retry.max_attempts
                    and (retry_http or isinstance(exc, TransportFailed))
                )
                if not should_retry and not isinstance(exc, RateLimited):
                    raise
                delay = self._retry.delay(attempt, retry_after=retry_after, rng=self._rng)
                if isinstance(exc, RateLimited):
                    self._limiter.penalize(delay)
                if not should_retry:
                    raise
                self._sleep(delay)
                if recover is not None and (result := recover()) is not None:
                    return result
                attempt += 1

    def _session_url(self, url: str, *, location: httpx.URL | None = None) -> httpx.URL:
        try:
            target = (location or self._api_base).join(url)
        except httpx.InvalidURL as exc:
            raise DriveError("Invalid resumable upload URL") from exc
        origin = self._api_base
        if (
            (target.scheme, target.host, target.port) != (origin.scheme, origin.host, origin.port)
            or target.userinfo
            or target.fragment
        ):
            raise DriveError("Invalid resumable upload origin")
        return target

    def _metadata_write(
        self,
        method: str,
        path: str,
        metadata: Mapping[str, object],
        *,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        **params: str,
    ) -> RemoteFile:
        return self._request(
            method,
            path,
            _read_file,
            kind="write",
            params={"fields": FILE_FIELDS, **params},
            json_body=metadata,
            before_send=before_send,
            should_stop=should_stop,
        )

    def about(self) -> About:
        """Return the account address and Drive quota. A missing limit means unlimited."""
        data = self._request(
            "GET",
            "/drive/v3/about",
            _read_json,
            params={"fields": "user(emailAddress),storageQuota(limit,usage,usageInDriveTrash)"},
        )
        quota = data.get("storageQuota") or {}
        limit = quota.get("limit")
        return About(
            (data.get("user") or {}).get("emailAddress"),
            int(limit) if limit is not None else None,
            int(quota.get("usage", 0)),
            int(quota.get("usageInDriveTrash", 0)),
        )

    def get(self, file_id: str) -> RemoteFile | None:
        """Fetch one file's metadata, returning None for HTTP 404."""
        try:
            return self._request(
                "GET",
                f"{_FILES}/{quote(file_id, safe='')}",
                _read_file,
                params={"fields": FILE_FIELDS},
            )
        except NotFound:
            return None

    def live_folder_in_subtree(
        self, folder_id: str, root_id: str, *, rel: str | None = None
    ) -> bool:
        """Check current parent links and, when given, the expected relative path."""
        seen: set[str] = set()
        names: list[str] = []
        while folder_id not in seen:
            seen.add(folder_id)
            folder = self.get(folder_id)
            if folder is None or folder.trashed or not folder.is_folder:
                break
            if folder.id == root_id or folder_id == root_id:
                return rel is None or "/".join(reversed(names)) == rel
            if len(folder.parents) != 1:
                break
            names.append(_rel_component(folder.name))
            folder_id = folder.parents[0]
        return False

    def list_children(
        self,
        parent_ids: Sequence[str],
        *,
        include_trashed: bool = False,
        page_size: int = 1000,
        name: str | None = None,
    ) -> Iterator[RemoteFile]:
        """Yield all direct children of 1 to 40 parents, or raise ValueError for other counts."""
        if not 1 <= len(parent_ids) <= LIST_GROUP_MAX:
            raise ValueError(f"Expected 1 to {LIST_GROUP_MAX} parent IDs")
        clauses = " or ".join(f"{_literal(parent_id)} in parents" for parent_id in parent_ids)
        query = f"({clauses})"
        if not include_trashed:
            query += " and trashed = false"
        if name is not None:
            query += f" and name = {_literal(name)}"
        params: dict[str, str | int] = {
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "q": query,
            "spaces": "drive",
            "pageSize": page_size,
        }
        return self._child_pages(params)

    def _child_pages(self, params: dict[str, str | int]) -> Iterator[RemoteFile]:
        while True:
            data = self._request("GET", _FILES, _read_json, params=params)
            for item in data.get("files", ()):
                yield RemoteFile.from_api(item)
            if not (token := data.get("nextPageToken")):
                return
            params["pageToken"] = token

    def generate_ids(self, count: int) -> list[str]:
        """Allocate 1 to 1000 IDs for retry-safe creates. Raise ValueError for other counts."""
        if not 1 <= count <= 1000:
            raise ValueError("Expected 1 to 1000 file IDs")
        data = self._request(
            "GET",
            f"{_FILES}/generateIds",
            _read_json,
            params={"count": count, "space": "drive", "type": "files", "fields": "ids"},
        )
        return [str(file_id) for file_id in data["ids"]]

    def start_page_token(self) -> str:
        """Fetch a token for subsequent changes to the user's Drive."""
        data = self._request(
            "GET", f"{_CHANGES}/startPageToken", _read_json, params={"fields": "startPageToken"}
        )
        return str(data["startPageToken"])

    def changes(
        self, page_token: str, *, page_size: int = 1000, single_attempt: bool = False
    ) -> ChangesPage:
        """Fetch one page with removals and the next cursor, optionally without retries."""
        data = self._request(
            "GET",
            _CHANGES,
            _read_json,
            params={
                "fields": _CHANGE_FIELDS,
                "pageToken": page_token,
                "pageSize": page_size,
                "spaces": "drive",
                "includeRemoved": "true",
            },
            single_attempt=single_attempt,
        )
        changes = tuple(
            Change(
                str(item["fileId"]),
                bool(item.get("removed", False)),
                RemoteFile.from_api(raw) if (raw := item.get("file")) is not None else None,
            )
            for item in data.get("changes", ())
        )
        return ChangesPage(changes, data.get("nextPageToken"), data.get("newStartPageToken"))

    def create_folder(
        self,
        *,
        file_id: str,
        name: str,
        parent_id: str,
        properties: Mapping[str, str],
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RemoteFile:
        """Create by generated ID, or return a matching folder after HTTP 409.

        Raise Conflict if the occupied ID does not identify the requested folder.
        """
        if not file_id:
            raise ValueError("Folder creation requires a generated file ID")
        try:
            return self._metadata_write(
                "POST",
                _FILES,
                {
                    "id": file_id,
                    "name": name,
                    "mimeType": FOLDER_MIME,
                    "parents": [parent_id],
                    "properties": dict(properties),
                },
                before_send=before_send,
                should_stop=should_stop,
            )
        except Conflict:
            item = self.get(file_id)
            if (
                item is not None
                and item.id == file_id
                and not item.trashed
                and item.is_folder
                and item.name == name
            ):
                if parent_id == "root" and item.parents != (parent_id,):
                    root = self.get(parent_id)
                    if root is not None:
                        parent_id = root.id
                if item.parents == (parent_id,):
                    if before_send is not None:
                        before_send()
                    return item
            raise

    def update_metadata(
        self,
        file_id: str,
        *,
        name: str | None = None,
        modified_ms: int | None = None,
        properties: Mapping[str, str] | None = None,
        add_parent: str | None = None,
        remove_parents: Sequence[str] = (),
        trashed: bool | None = None,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RemoteFile:
        """Patch the supplied metadata and parent changes, returning the updated file."""
        metadata: dict[str, object] = {}
        if name is not None:
            metadata["name"] = name
        if modified_ms is not None:
            metadata["modifiedTime"] = rfc3339_from_ms(modified_ms)
        if properties is not None:
            metadata["properties"] = dict(properties)
        if trashed is not None:
            metadata["trashed"] = trashed
        params: dict[str, str] = {}
        if add_parent is not None:
            params["addParents"] = add_parent
        if remove_parents:
            params["removeParents"] = ",".join(remove_parents)
        return self._metadata_write(
            "PATCH",
            f"{_FILES}/{quote(file_id, safe='')}",
            metadata,
            before_send=before_send,
            should_stop=should_stop,
            **params,
        )

    def multipart_upload(
        self,
        *,
        data: bytes,
        file_id: str,
        name: str | None,
        parent_id: str | None,
        modified_ms: int,
        properties: Mapping[str, str] | None,
        update: bool,
    ) -> RemoteFile:
        """Send metadata and bytes together. A create with an occupied ID raises Conflict."""
        return self._multipart(
            data=data,
            file_id=file_id,
            name=name,
            parent_id=parent_id,
            modified_ms=modified_ms,
            properties=properties,
            update=update,
        )

    def _multipart(
        self,
        *,
        data: bytes,
        file_id: str,
        name: str | None,
        parent_id: str | None,
        modified_ms: int,
        properties: Mapping[str, str] | None,
        update: bool,
        on_send: Callable[[int], None] | None = None,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        validate_recovered: Callable[[RemoteFile], None] | None = None,
    ) -> RemoteFile:
        metadata = _media_metadata(file_id, name, parent_id, modified_ms, properties, update)
        metadata_bytes = json.dumps(metadata, ensure_ascii=False).encode()
        boundary = uuid4().hex
        while boundary.encode() in data or boundary.encode() in metadata_bytes:
            boundary = uuid4().hex
        prefix = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + metadata_bytes
            + f"\r\n--{boundary}\r\nContent-Type: {_BINARY}\r\n\r\n".encode()
        )
        suffix = f"\r\n--{boundary}--\r\n".encode()
        path = f"{_UPLOAD}/{quote(file_id, safe='')}" if update else _UPLOAD

        def recover_update() -> RemoteFile | None:
            item = self.get(file_id)
            if (
                item is not None
                and not item.trashed
                and not item.is_folder
                and not item.is_google_native
                and item.size == len(data)
                and item.md5 == hashlib.md5(data, usedforsecurity=False).hexdigest()
                and item.modified_ms == modified_ms
                and all(
                    item.properties.get(key) == value for key, value in (properties or {}).items()
                )
            ):
                if validate_recovered is not None:
                    validate_recovered(item)
                return item
            return None

        return self._request(
            "PATCH" if update else "POST",
            path,
            _read_file,
            kind="write",
            params={"fields": FILE_FIELDS, "uploadType": "multipart"},
            headers={
                "Content-Type": f"multipart/related; boundary={boundary}",
                "Content-Length": str(len(prefix) + len(data) + len(suffix)),
            },
            content=lambda: _media_body(data, on_send, prefix=prefix, suffix=suffix),
            recover=recover_update if update else None,
            before_send=before_send,
            should_stop=should_stop,
        )

    def start_resumable(
        self,
        *,
        size: int,
        file_id: str,
        name: str | None,
        parent_id: str | None,
        modified_ms: int,
        properties: Mapping[str, str] | None,
        update: bool,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """Open a resumable upload and return its session URL for durable storage."""
        metadata = _media_metadata(file_id, name, parent_id, modified_ms, properties, update)
        path = f"{_UPLOAD}/{quote(file_id, safe='')}" if update else _UPLOAD

        def session_location(response: httpx.Response) -> str:
            location = response.headers.get("Location")
            if not location:
                raise DriveError("Drive did not return a resumable upload URL")
            return str(self._session_url(location, location=response.url))

        return self._request(
            "PATCH" if update else "POST",
            path,
            session_location,
            kind="write",
            params={"fields": FILE_FIELDS, "uploadType": "resumable"},
            headers={"X-Upload-Content-Type": _BINARY, "X-Upload-Content-Length": str(size)},
            json_body=metadata,
            before_send=before_send,
            should_stop=should_stop,
        )

    def resumable_status(
        self,
        url: str,
        size: int,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int | RemoteFile:
        """Return the next confirmed offset or the completed file. Raise SessionExpired on 404."""
        return self._request(
            "PUT",
            self._session_url(url),
            lambda response: _read_resumable(response, size),
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            content=b"",
            session=True,
            should_stop=should_stop,
        )

    def resumable_put(self, url: str, chunk: bytes, offset: int, size: int) -> int | RemoteFile:
        """Send a chunk and return an offset or the finished file. Raise SessionExpired on 404."""
        return self._put_chunk(url, chunk, offset, size)

    def _put_chunk(
        self,
        url: str,
        chunk: bytes,
        offset: int,
        size: int,
        *,
        on_send: Callable[[int], None] | None = None,
        recover: Callable[[], int | RemoteFile | None] | None = None,
        before_send: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> int | RemoteFile:
        if not (size == offset == len(chunk) == 0 or 0 <= offset < offset + len(chunk) <= size):
            raise ValueError("Chunk does not fit the upload bounds")
        byte_range = f"{offset}-{offset + len(chunk) - 1}" if chunk else "*"

        def result(response: httpx.Response) -> int | RemoteFile:
            position = _read_resumable(response, size)
            if recover is not None and position == offset:
                raise ServerError("Drive did not advance the resumable upload", status=308)
            return position

        return self._request(
            "PUT",
            self._session_url(url),
            result,
            kind="write",
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {byte_range}/{size}",
            },
            content=lambda: _media_body(chunk, on_send),
            session=True,
            retry_http=recover is not None,
            recover=recover,
            before_send=before_send,
            should_stop=should_stop,
        )

    def download(
        self, file_id: str, sink: BinaryIO, *, on_bytes: Callable[[int], None] | None = None
    ) -> None:
        """Stream bytes to a binary sink, accounting for each successful write.

        A retried interrupted stream requests the remaining range. The sink is
        caller-owned and can contain partial data if the operation fails.
        """
        offset = 0
        headers = {"Accept-Encoding": "identity"}

        def write(response: httpx.Response) -> None:
            nonlocal offset
            expected_end: int | None = None
            if offset:
                value = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})", value)
                if (
                    response.status_code != 206
                    or match is None
                    or int(match[1]) != offset
                    or not offset <= int(match[2]) == int(match[3]) - 1
                ):
                    raise DriveError("Drive did not send the requested download range")
                expected_end = int(match[3])
            elif (etag := response.headers.get("ETag")) and not etag.startswith("W/"):
                headers["If-Range"] = etag
            for chunk in response.iter_bytes():
                if sink.write(chunk) != len(chunk):
                    raise OSError("Short write to download sink")
                offset += len(chunk)
                headers["Range"] = f"bytes={offset}-"
                if on_bytes is not None:
                    on_bytes(len(chunk))
            if expected_end is not None and offset != expected_end:
                raise DriveError("Download length disagrees with Content-Range")

        self._request(
            "GET",
            f"{_FILES}/{quote(file_id, safe='')}",
            write,
            params={"alt": "media"},
            headers=headers,
        )


def _read_json(response: httpx.Response) -> dict[str, Any]:
    response.read()
    try:
        data = response.json()
    except ValueError as exc:
        raise DriveError("Drive returned invalid JSON", status=response.status_code) from exc
    if not isinstance(data, dict):
        raise DriveError("Drive did not return a JSON object")
    return data


def _read_file(response: httpx.Response) -> RemoteFile:
    raw = _read_json(response)
    try:
        return RemoteFile.from_api(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise DriveError(
            "Drive returned invalid file metadata", status=response.status_code
        ) from exc


def _read_resumable(response: httpx.Response, size: int) -> int | RemoteFile:
    if response.status_code != 308:
        return _read_file(response)
    value = response.headers.get("Range")
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-([0-9]{1,19})", value)
    if match is None or (offset := int(match[1]) + 1) >= size:
        raise DriveError("Invalid resumable upload Range", status=308)
    return offset
