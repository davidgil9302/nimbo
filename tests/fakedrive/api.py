"""Public fake and the request handler shared by both transports."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import httpx

from nimbo.core.model import FOLDER_MIME, GOOGLE_APPS_PREFIX

from . import fields, query
from .faults import CallRecord, FaultPlan, SimulatedCrash
from .oauth import AUTH_URL, REVOKE_URL, TOKEN_URL, FakeOAuth
from .server import serve
from .state import BINARY_MIME, DriveAPIError, DriveState, FakeFile
from .uploads import UPLOAD_PATH, Uploads, decimal, metadata_request

_FILES = "/drive/v3/files"
_DOWNLOAD_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")
_STATUS_NAMES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    410: "FAILED_PRECONDITION",
    412: "FAILED_PRECONDITION",
    416: "OUT_OF_RANGE",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


@dataclass(frozen=True, slots=True)
class _FilePage:
    expression: str | None
    spaces: frozenset[str]
    after_id: str


class FakeDrive:
    """An isolated, authenticated subset of Google Drive v3 and Google OAuth."""

    API_BASE = "https://www.googleapis.com"
    TOKEN_URL = TOKEN_URL
    REVOKE_URL = REVOKE_URL
    AUTH_URL = AUTH_URL

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        quota_limit: int | None = 15 * 2**30,
        daily_upload_limit: int | None = None,
        writes_per_second: float | None = None,
    ) -> None:
        self._state = DriveState(clock, quota_limit, daily_upload_limit, writes_per_second)
        self._uploads = Uploads(self._state)
        self._file_pages: dict[str, _FilePage] = {}
        self.root_id = self._state.root_id
        self.calls: list[CallRecord] = []
        self.faults = FaultPlan(self._state.lock)
        self.oauth = FakeOAuth(self._state)

    def transport(self) -> httpx.MockTransport:
        """Return an HTTPX transport routing requests directly to this fake."""
        return httpx.MockTransport(self._handle)

    def client(self, **kwargs: Any) -> httpx.Client:
        """Return a client with the fake transport and a default Drive base URL."""
        kwargs.setdefault("base_url", self.API_BASE)
        return httpx.Client(transport=self.transport(), **kwargs)

    def serve(self) -> AbstractContextManager[str]:
        """Yield a loopback base URL backed by the same request handler and state."""
        return serve(self._handle)

    def now(self) -> float:
        """Return the fake's current seconds, including any injected delays."""
        return self._state.now()

    def add_folder(
        self,
        name: str,
        parent: str | None = None,
        *,
        device: bool = False,
        properties: Mapping[str, str] | None = None,
    ) -> str:
        """Seed a folder and return its ID, raising a Drive error for an invalid parent."""
        with self._state.lock:
            return self._state.seed(
                name,
                self.root_id if parent is None else parent,
                FOLDER_MIME,
                properties=properties,
                device=device,
            )

    def add_file(
        self,
        name: str,
        parent: str,
        content: bytes,
        *,
        modified_ms: int | None = None,
        properties: Mapping[str, str] | None = None,
        mime_type: str = BINARY_MIME,
    ) -> str:
        """Seed a binary file and return its ID, raising ValueError for native MIME types."""
        if mime_type.startswith(GOOGLE_APPS_PREFIX):
            raise ValueError("Use add_folder or add_native for Google Drive MIME types")
        with self._state.lock:
            return self._state.seed(
                name,
                parent,
                mime_type,
                content=content,
                modified_ms=modified_ms,
                properties=properties,
            )

    def add_native(
        self, name: str, parent: str, mime_type: str = "application/vnd.google-apps.document"
    ) -> str:
        """Seed a native Drive file with no media and return its ID."""
        if not mime_type.startswith(GOOGLE_APPS_PREFIX) or mime_type == FOLDER_MIME:
            raise ValueError("Expected a native Google Drive file MIME type")
        with self._state.lock:
            return self._state.seed(name, parent, mime_type)

    def set_content(self, file_id: str, content: bytes, *, keep_modified: bool = True) -> None:
        """Replace fixture bytes. Raise KeyError for absent IDs or ValueError for native files."""
        with self._state.lock:
            self._state.overwrite(file_id, content, keep_modified=keep_modified)

    def snapshot(self, root_id: str) -> dict[str, tuple[bool, int, str | None]]:
        """Map visible relative paths below a folder to (is_folder, size, md5)."""
        with self._state.lock:
            return self._state.snapshot(root_id)

    def find(self, parent_id: str, name: str) -> list[FakeFile]:
        """Return detached records for all matching direct children, including trashed ones."""
        with self._state.lock:
            return self._state.find(parent_id, name)

    def file(self, file_id: str) -> FakeFile:
        """Return a detached file record or raise KeyError if it does not exist."""
        with self._state.lock:
            return self._state.inspect(file_id)

    def write_count(self) -> int:
        """Count applied Drive mutations, including writes whose response was lost."""
        with self._state.lock:
            return self._state.writes

    def expire_upload_sessions(self) -> None:
        """Invalidate every upload session without changing any committed files."""
        with self._state.lock:
            self._uploads.expire()

    def _handle(self, request: httpx.Request, server_base: str | None = None) -> httpx.Response:
        with self._state.lock:
            response: httpx.Response | None = None
            try:
                fault = self.faults._next(request)
                if fault is not None:
                    if fault.action == "status":
                        response = _error(
                            DriveAPIError(fault.status, fault.reason, _status_message(fault.status))
                        )
                        return response
                    if fault.action == "transport_error":
                        raise httpx.ConnectError("Injected connection failure", request=request)
                    if fault.action == "crash_before":
                        raise SimulatedCrash("Injected crash before request")
                    if fault.action == "delay":
                        self._state.delay += fault.seconds
                try:
                    response = self._route(request, server_base)
                except DriveAPIError as err:
                    response = _error(err)
                if fault is not None:
                    if fault.action == "drop_response":
                        raise httpx.ReadTimeout("Injected lost response", request=request)
                    if fault.action == "crash_after":
                        raise SimulatedCrash("Injected crash after request")
                return response
            finally:
                self.calls.append(
                    CallRecord(
                        request.method,
                        request.url.path,
                        request.url.params,
                        None if response is None else response.status_code,
                    )
                )

    def _route(self, request: httpx.Request, server_base: str | None) -> httpx.Response:
        url, method = request.url, request.method
        path = url.path
        if (
            method == "POST"
            and path in {"/token", "/revoke"}
            and (server_base is not None or url.host == "oauth2.googleapis.com")
        ):
            return self.oauth._token(request) if path == "/token" else self.oauth._revoke(request)
        if (
            method == "GET"
            and path == "/o/oauth2/v2/auth"
            and (server_base is not None or url.host == "accounts.google.com")
        ):
            return self.oauth._authorization(request, server_base)

        if server_base is None and url.host != "www.googleapis.com":
            raise DriveAPIError(404, "notFound", "Unknown endpoint.")
        if path.startswith(("/drive/v3/", "/upload/drive/v3/")) and not self.oauth._accepts(
            request.headers.get("Authorization", "")
        ):
            raise DriveAPIError(401, "authError", "Invalid or missing authentication credentials.")
        params = url.params
        match path, method:
            case ("/drive/v3/about", "GET"):
                return self._about(params)
            case ("/drive/v3/files", "GET"):
                return self._list(params)
            case ("/drive/v3/files", "POST"):
                return self._write_file(request)
            case ("/drive/v3/files/generateIds", "GET"):
                return self._generate_ids(params)
            case ("/drive/v3/changes/startPageToken", "GET"):
                data = {
                    "kind": "drive#startPageToken",
                    "startPageToken": str(len(self._state.changes)),
                }
                return _json(data, params, fields.START_TOKEN, "startPageToken")
            case ("/drive/v3/changes", "GET"):
                return self._changes(params)
        upload_id = _resource_id(path, UPLOAD_PATH)
        if path == UPLOAD_PATH or upload_id is not None:
            if method == "PUT":
                return self._uploads.put(request)
            if (method == "POST" and upload_id is None) or (
                method == "PATCH" and upload_id is not None
            ):
                mask = _mask(params, fields.FILE, fields.DEFAULT_FILE)
                return self._uploads.initiate(
                    request, upload_id, server_base or self.API_BASE, mask
                )
        file_id = _resource_id(path, _FILES)
        if file_id is not None and file_id != "generateIds":
            if method == "GET":
                return self._get_file(request, file_id)
            if method == "PATCH":
                return self._write_file(request, file_id)
            if method == "DELETE":
                self._state.delete(file_id)
                return httpx.Response(204)
        raise DriveAPIError(404, "notFound", "Unknown endpoint.")

    def _about(self, params: httpx.QueryParams) -> httpx.Response:
        user = {
            "kind": "drive#user",
            "displayName": "Drive Test User",
            "emailAddress": "drive-user@example.test",
            "permissionId": "fake-user",
            "me": True,
        }
        data = {"kind": "drive#about", "user": user, "storageQuota": self._state.storage()}
        return _json(data, params, fields.ABOUT, "*")

    def _generate_ids(self, params: httpx.QueryParams) -> httpx.Response:
        count = _bounded_decimal(params.get("count", "10"), 1, 1000, "count")
        if params.get("space", "drive") != "drive" or params.get("type", "files") != "files":
            raise DriveAPIError.bad_request("Expected space=drive and type=files.")
        mask = _mask(params, fields.GENERATED_IDS, "*")
        data = {
            "kind": "drive#generatedIds",
            "space": "drive",
            "ids": self._state.generate_ids(count),
        }
        return httpx.Response(200, json=fields.select(data, mask))

    def _get_file(self, request: httpx.Request, file_id: str) -> httpx.Response:
        item = self._state.require(file_id)
        params = request.url.params
        alt = params.get("alt", "json")
        if alt == "media":
            return _download(item, request.headers.get("Range"))
        if alt != "json":
            raise DriveAPIError.bad_request("Unsupported alt value.")
        return _json(item.as_api(), params, fields.FILE, fields.DEFAULT_FILE)

    def _write_file(self, request: httpx.Request, file_id: str | None = None) -> httpx.Response:
        params = request.url.params
        mask = _mask(params, fields.FILE, fields.DEFAULT_FILE)
        item = self._state.prepare(
            metadata_request(request),
            file_id,
            add_parents=params.get("addParents"),
            remove_parents=params.get("removeParents"),
        )
        self._state.check_write()
        saved = self._state.commit(item)
        self._state.record_write()
        return httpx.Response(200, json=fields.select(saved.as_api(), mask))

    def _list(self, params: httpx.QueryParams) -> httpx.Response:
        expression = params.get("q")
        try:
            predicate = query.predicate(expression, self._state.resolve_id)
        except ValueError as err:
            raise DriveAPIError(400, "invalid", str(err)) from err
        page_size = _page_size(params)
        spaces = _spaces(params)
        mask = _mask(params, fields.FILES, fields.DEFAULT_FILES)
        after_id = ""
        if "pageToken" in params:
            page = self._file_pages.get(params["pageToken"])
            if page is None or (page.expression, page.spaces) != (expression, spaces):
                raise DriveAPIError(400, "invalid", "Invalid page token.")
            after_id = page.after_id
        matches: list[FakeFile] = []
        if "drive" in spaces:
            matches = sorted(
                (
                    item
                    for item in self._state.files.values()
                    if item.id > after_id and item.id != self.root_id and predicate(item)
                ),
                key=lambda item: item.id,
            )
        response: dict[str, Any] = {
            "kind": "drive#fileList",
            "files": [item.as_api() for item in matches[:page_size]],
        }
        if page_size < len(matches):
            token = uuid4().hex
            self._file_pages[token] = _FilePage(expression, spaces, matches[page_size - 1].id)
            response["nextPageToken"] = token
        return httpx.Response(200, json=fields.select(response, mask))

    def _changes(self, params: httpx.QueryParams) -> httpx.Response:
        changes = self._state.changes
        start = _bounded_decimal(params.get("pageToken", ""), 0, len(changes), "pageToken")
        page_size = _page_size(params)
        spaces = _spaces(params)
        include_removed = params.get("includeRemoved", "true")
        if include_removed not in {"true", "false"}:
            raise DriveAPIError.bad_request("Invalid includeRemoved value.")
        items: list[dict[str, Any]] = []
        response: dict[str, Any] = {"kind": "drive#changeList", "changes": items}
        for index in range(start, len(changes)) if "drive" in spaces else ():
            change = changes[index]
            if change["removed"] and include_removed == "false":
                continue
            if len(items) == page_size:
                response["nextPageToken"] = str(index)
                break
            items.append(change)
        else:
            response["newStartPageToken"] = str(len(changes))
        return _json(response, params, fields.CHANGES, "*")


def _resource_id(path: str, collection: str) -> str | None:
    prefix = collection + "/"
    if path.startswith(prefix):
        suffix = path[len(prefix) :]
        if suffix and "/" not in suffix:
            return suffix
    return None


def _bounded_decimal(value: str, lower: int, upper: int, label: str) -> int:
    try:
        number = decimal(value)
    except ValueError as err:
        raise DriveAPIError.bad_request(f"Invalid {label}.") from err
    if not lower <= number <= upper:
        raise DriveAPIError.bad_request(f"Invalid {label}.")
    return number


def _page_size(params: httpx.QueryParams) -> int:
    return _bounded_decimal(params.get("pageSize", "100"), 1, 1000, "pageSize")


def _spaces(params: httpx.QueryParams) -> frozenset[str]:
    spaces = frozenset(params.get("spaces", "drive").split(","))
    if spaces - {"drive", "appDataFolder"}:
        raise DriveAPIError.bad_request("Invalid spaces.")
    return spaces


def _mask(params: httpx.QueryParams, schema: fields.Schema, default: str) -> fields.Mask:
    try:
        return fields.parse(params.get("fields", default), schema)
    except ValueError as err:
        raise DriveAPIError(400, "invalid", str(err)) from err


def _json(
    data: dict[str, Any], params: httpx.QueryParams, schema: fields.Schema, default: str
) -> httpx.Response:
    return httpx.Response(200, json=fields.select(data, _mask(params, schema, default)))


def _download(item: FakeFile, byte_range: str | None) -> httpx.Response:
    if not item.is_binary:
        raise DriveAPIError(403, "fileNotDownloadable", "Only binary files can be downloaded.")
    headers = {"Content-Type": item.mime_type, "Accept-Ranges": "bytes"}
    if byte_range is None:
        return httpx.Response(200, content=item.content, headers=headers)
    size = len(item.content)
    try:
        start, end = _download_bounds(byte_range, size)
    except ValueError:
        return _error(
            DriveAPIError(416, "requestedRangeNotSatisfiable", "Requested range not satisfiable."),
            headers={"Content-Range": f"bytes */{size}"},
        )
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return httpx.Response(206, content=item.content[start : end + 1], headers=headers)


def _download_bounds(byte_range: str, size: int) -> tuple[int, int]:
    match = _DOWNLOAD_RANGE.fullmatch(byte_range)
    if match is None:
        raise ValueError("Invalid download range")
    left, right = match.groups()
    if not left:
        start, end = max(0, size - decimal(right)), size - 1
    else:
        start, end = decimal(left), min(decimal(right), size - 1) if right else size - 1
    if start > end:
        raise ValueError("Unsatisfiable download range")
    return start, end


def _status_message(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Injected HTTP status"


def _error(err: DriveAPIError, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
    rate_reasons = {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
        "storageQuotaExceeded",
        "quotaExceeded",
        "uploadLimitExceeded",
        "dailyLimitExceeded",
    }
    domain = "usageLimits" if err.reason in rate_reasons else "global"
    message = str(err)
    detail = {
        "code": err.status,
        "message": message,
        "errors": [{"domain": domain, "reason": err.reason, "message": message}],
        "status": _STATUS_NAMES.get(err.status, "UNKNOWN"),
    }
    return httpx.Response(err.status, json={"error": detail}, headers=headers)
