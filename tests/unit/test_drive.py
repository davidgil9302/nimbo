"""Drive v3 requests, error classification, and retries against the fake."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Literal

import httpx
import pytest

from nimbo.core import drive as drive_module
from nimbo.core.drive import LIST_GROUP_MAX, DriveClient
from nimbo.core.errors import (
    BadRequest,
    Conflict,
    DriveError,
    Forbidden,
    NotFound,
    RateLimited,
    ServerError,
    SessionExpired,
    StorageQuotaExceeded,
    TransportFailed,
    Unauthorized,
    UploadLimitExceeded,
)
from nimbo.core.model import FILE_FIELDS, FOLDER_MIME, About
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from tests.fakedrive import FakeDrive, Fault, FaultPlan


class _Tokens:
    def __init__(self, *tokens: str) -> None:
        self.tokens = tokens
        self.invalidations = 0

    def token(self) -> str:
        """Return the current token, advancing after invalidation."""
        return self.tokens[min(self.invalidations, len(self.tokens) - 1)]

    def invalidate(self) -> None:
        """Record an invalidation."""
        self.invalidations += 1


class _Limiter(RateLimiter):
    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.penalties: list[float] = []

    def acquire(self, kind: Literal["read", "write"]) -> None:
        """Record the bucket that would be charged."""
        self.kinds.append(kind)

    def penalize(self, seconds: float) -> None:
        """Record the length of the shared pause."""
        self.penalties.append(seconds)


class _Harness:
    def __init__(
        self,
        fake: FakeDrive,
        http: httpx.Client,
        *,
        tokens: _Tokens | None = None,
        attempts: int = 3,
    ) -> None:
        self.fake = fake
        self.tokens = tokens or _Tokens(fake.oauth.add_access_token("test-token"))
        self.limiter = _Limiter()
        self.sleeps: list[float] = []
        self.drive = DriveClient(
            http,
            self.tokens,
            self.limiter,
            RetryPolicy(max_attempts=attempts),
            rng=random.Random(17),
            sleep=self.sleeps.append,
        )


@pytest.fixture
def harness() -> Iterator[_Harness]:
    """Provide an authenticated client with an observable limiter and clock."""
    fake = FakeDrive()
    with fake.client() as http:
        yield _Harness(fake, http)


@pytest.mark.parametrize("limit", [None, 100])
def test_about_parses_quota_and_unlimited(limit: int | None) -> None:
    """Decode the optional limit and count both live and trashed content."""
    fake = FakeDrive(quota_limit=limit)
    with fake.client() as http:
        drive = _Harness(fake, http).drive
        fake.add_file("active", fake.root_id, b"data")
        trash = fake.add_file("trash", fake.root_id, b"old")
        drive.update_metadata(trash, trashed=True)
        about = drive.about()
        assert about == About("drive-user@example.test", limit, 7, 3)
        assert about.free == (None if limit is None else 93)


def test_get_returns_none_on_404(harness: _Harness) -> None:
    """Treat missing file metadata as a normal lookup miss."""
    assert harness.drive.get("missing") is None
    assert [call.status for call in harness.fake.calls] == [404]


def test_list_children_groups_and_paginates(harness: _Harness) -> None:
    """Group the parent clauses and preserve the query across pages."""
    fake, drive = harness.fake, harness.drive
    first, second, outside = (fake.add_folder(name) for name in ("one", "two", "other"))
    expected = [fake.add_file("a", first, b"a"), fake.add_native("b", second)]
    fake.add_file("c", outside, b"c")
    trashed = fake.add_file("d", first, b"d")
    drive.update_metadata(trashed, trashed=True)
    fake.calls.clear()

    assert {item.id for item in drive.list_children([first, second], page_size=1)} == set(expected)
    assert len(fake.calls) == 2
    assert fake.calls[1].query["pageToken"]
    for call in fake.calls:
        assert (
            call.query["q"]
            == f"('{first}' in parents or '{second}' in parents) and trashed = false"
        )
        assert (call.query["spaces"], call.query["pageSize"]) == ("drive", "1")
    assert {item.id for item in drive.list_children([first, second], include_trashed=True)} == {
        *expected,
        trashed,
    }


@pytest.mark.parametrize("count", [0, LIST_GROUP_MAX + 1])
def test_list_children_rejects_too_many_ids(harness: _Harness, count: int) -> None:
    """Reject empty lists and groups larger than the supported bound."""
    with pytest.raises(ValueError):
        harness.drive.list_children(["id"] * count)
    assert not harness.fake.calls


def test_q_escaping_of_ids(harness: _Harness) -> None:
    """Send Drive backslash and quote escapes outside URL encoding."""
    assert list(harness.drive.list_children(["a'b\\c", "two"])) == []
    assert (
        harness.fake.calls[-1].query["q"]
        == r"('a\'b\\c' in parents or 'two' in parents) and trashed = false"
    )


def test_requests_only_needed_fields(harness: _Harness) -> None:
    """Verify masks for resources, uploads, and the response-free endpoints."""
    drive, fake = harness.drive, harness.fake
    drive.about()
    drive.start_page_token()
    drive.changes("0")
    list(drive.list_children([fake.root_id]))
    folder_id, file_id, resumable_id = drive.generate_ids(3)
    drive.create_folder(file_id=folder_id, name="folder", parent_id=fake.root_id, properties={})
    drive.get(folder_id)
    drive.update_metadata(folder_id, name="renamed")
    create = dict(name="file", parent_id=folder_id, modified_ms=0, properties={}, update=False)
    drive.multipart_upload(file_id=file_id, data=b"x", **create)
    url = drive.start_resumable(file_id=resumable_id, size=1, **create)
    drive.resumable_status(url, 1)
    drive.resumable_put(url, b"x", 0, 1)
    drive.download(file_id, BytesIO())
    assert [call.query.get("fields") for call in fake.calls] == [
        "user(emailAddress),storageQuota(limit,usage,usageInDriveTrash)",
        "startPageToken",
        f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))",
        f"nextPageToken,files({FILE_FIELDS})",
        "ids",
        *[FILE_FIELDS] * 5,
        None,
        None,
        None,
    ]
    assert fake.calls[-1].query["alt"] == "media"
    assert fake.calls[4].query["space"] == "drive"
    assert fake.calls[4].query["type"] == "files"


def test_generate_ids_bounds(harness: _Harness) -> None:
    """Check both boundaries and do client-side validation before requesting IDs."""
    for count in (0, 1001):
        with pytest.raises(ValueError):
            harness.drive.generate_ids(count)
    assert not harness.fake.calls
    one = harness.drive.generate_ids(1)
    full = harness.drive.generate_ids(1000)
    assert len(one) == 1 and len(full) == 1000 and len(set(one + full)) == 1001


def test_create_folder_idempotent_after_dropped_response(harness: _Harness) -> None:
    """Recover the original folder by its pre-generated ID after a response is lost."""
    fake, drive = harness.fake, harness.drive
    (file_id,) = drive.generate_ids(1)
    fake.faults.add(Fault(FaultPlan.is_write, "drop_response"))
    item = drive.create_folder(
        file_id=file_id, name="docs", parent_id=fake.root_id, properties={"nimbo": "owner"}
    )
    assert (item.id, item.mime_type, item.properties) == (file_id, FOLDER_MIME, {"nimbo": "owner"})
    assert [item.id for item in fake.find(fake.root_id, "docs")] == [file_id]
    assert [(call.method, call.status) for call in fake.calls[1:]] == [
        ("POST", 200),
        ("POST", 409),
        ("GET", 200),
    ]


@pytest.mark.parametrize("different", ["name", "parent", "type", "trashed"])
def test_create_folder_conflict_with_different_item_raises(
    harness: _Harness, different: str
) -> None:
    """Reject ID collisions with the wrong name, parent or MIME type."""
    fake, drive = harness.fake, harness.drive
    if different == "type":
        occupied = fake.add_file("docs", fake.root_id, b"x")
    elif different == "name":
        occupied = fake.add_folder("other")
    elif different == "trashed":
        occupied = fake.add_folder("docs")
        drive.update_metadata(occupied, trashed=True)
    else:
        occupied = fake.add_folder("docs", fake.add_folder("elsewhere"))
    with pytest.raises(Conflict, match="already in use"):
        drive.create_folder(file_id=occupied, name="docs", parent_id=fake.root_id, properties={})


def test_update_metadata_move_rename_trash(harness: _Harness) -> None:
    """Move, rename, set settable metadata, and trash or untrash one ID."""
    fake, drive = harness.fake, harness.drive
    dest = fake.add_folder("dest")
    file_id = fake.add_file("old", fake.root_id, b"x", properties={"existing": "value"})
    item = drive.update_metadata(
        file_id,
        name="new",
        add_parent=dest,
        remove_parents=[fake.root_id],
        modified_ms=1234,
        properties={"nimbo": "owner"},
        trashed=True,
    )
    assert (item.id, item.name, item.modified_ms, item.parents, item.trashed) == (
        file_id,
        "new",
        1234,
        (dest,),
        True,
    )
    assert item.properties == {"existing": "value", "nimbo": "owner"}
    assert not drive.update_metadata(file_id, trashed=False).trashed


@pytest.mark.parametrize(
    ("status", "reason", "message", "expected"),
    [
        (400, "invalid", "bad request", BadRequest),
        (401, "authError", "expired", Unauthorized),
        (403, "rateLimitExceeded", "rate", RateLimited),
        (403, "userRateLimitExceeded", "rate", RateLimited),
        (403, "sharingRateLimitExceeded", "rate", RateLimited),
        (403, "storageQuotaExceeded", "storage", StorageQuotaExceeded),
        (403, "quotaExceeded", "storage", StorageQuotaExceeded),
        (403, "uploadLimitExceeded", "daily", UploadLimitExceeded),
        (403, "other", "Daily UPLOAD LIMIT reached", UploadLimitExceeded),
        (403, "insufficientFilePermissions", "forbidden", Forbidden),
        (404, "notFound", "absent", NotFound),
        (409, "fileIdInUse", "conflict", Conflict),
        (429, "other", "slow down", RateLimited),
        (500, "backendError", "server error", ServerError),
        (503, "backendError", "unavailable", ServerError),
        (599, "backendError", "server error", ServerError),
    ],
)
def test_error_mapping_table(
    status: int, reason: str, message: str, expected: type[DriveError]
) -> None:
    """Preserve the API status, reason, and detail for every mapped response."""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            status, json={"error": {"message": message, "errors": [{"reason": reason}]}}
        )
    )
    with httpx.Client(transport=transport) as http:
        harness = _Harness(FakeDrive(), http, attempts=1)
        with pytest.raises(expected) as raised:
            harness.drive.about()
    assert type(raised.value) is expected
    assert (raised.value.status, raised.value.reason, raised.value.detail) == (
        status,
        reason,
        message,
    )


def test_session_404_is_expired(harness: _Harness) -> None:
    """Distinguish expired upload capabilities from missing files."""
    with pytest.raises(SessionExpired):
        harness.drive.resumable_status(
            FakeDrive.API_BASE + "/upload/drive/v3/files?upload_id=gone", 1
        )


def test_retries_rate_limit_with_backoff_and_penalty(harness: _Harness) -> None:
    """Apply the policy's deterministic jitter and penalize all limiter buckets."""
    harness.fake.faults.add(
        Fault(lambda _: True, "status", status=403, reason="userRateLimitExceeded", times=2)
    )
    assert harness.drive.about().usage == 0
    rng = random.Random(17)
    assert harness.sleeps == harness.limiter.penalties == [rng.uniform(0, 1), rng.uniform(0, 2)]
    assert harness.limiter.kinds == ["read"] * 3


@pytest.mark.parametrize(
    ("header", "delay"), [("7", 7), ("900", 300), ("Mon, 23 Nov 2026 20:00:11 GMT", 11)]
)
def test_retry_after_header_honored(
    header: str, delay: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Respect Retry-After and its policy cap for errors that are retried."""

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            """Return the fixed reference time for HTTP-date retry headers."""
            return cls(2026, 11, 23, 20, tzinfo=UTC)

    monkeypatch.setattr(drive_module, "datetime", FixedDateTime)
    fake = FakeDrive()
    transport = fake.transport()
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": header}, text="busy")
        return transport.handle_request(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        harness = _Harness(fake, http)
        harness.drive.about()
    assert harness.sleeps == harness.limiter.penalties == [delay]


def test_transport_errors_retried_then_raised(harness: _Harness) -> None:
    """Map persistent transport errors after the configured attempt budget."""
    harness.fake.faults.add(Fault(lambda _: True, "transport_error", times=3))
    with pytest.raises(TransportFailed):
        harness.drive.about()
    assert len(harness.fake.calls) == 3 and len(harness.sleeps) == 2


@pytest.mark.parametrize("refreshed_works", [True, False])
def test_unauthorized_invalidates_and_retries_once(refreshed_works: bool) -> None:
    """Invalidate once even when there is only one ordinary retry attempt."""
    fake = FakeDrive()
    initial = fake.oauth.add_access_token("expired", expires_in=0)
    next_token = fake.oauth.add_access_token("refreshed") if refreshed_works else "invalid"
    with fake.client() as http:
        harness = _Harness(fake, http, tokens=_Tokens(initial, next_token), attempts=1)
        if refreshed_works:
            assert harness.drive.about().usage == 0
        else:
            with pytest.raises(Unauthorized):
                harness.drive.about()
    assert harness.tokens.invalidations == 1
    assert len(fake.calls) == 2 and not harness.sleeps


def test_changes_page_parsing(harness: _Harness) -> None:
    """Read both token types and preserve removals without file resources."""
    drive, fake = harness.drive, harness.fake
    token = drive.start_page_token()
    file_id = fake.add_file("notes", fake.root_id, b"contents")
    with fake.client(headers={"Authorization": f"Bearer {harness.tokens.token()}"}) as http:
        assert http.delete(f"/drive/v3/files/{file_id}").status_code == 204
    page = drive.changes(token, page_size=1)
    assert page.next_page_token is not None and page.new_start_page_token is None
    (created,) = page.changes
    assert created.file_id == file_id and not created.removed and created.file is not None
    assert created.file.size == 8 and created.file.name == "notes"
    page = drive.changes(page.next_page_token)
    assert page.next_page_token is None and page.new_start_page_token == drive.start_page_token()
    (removed,) = page.changes
    assert (removed.file_id, removed.removed, removed.file) == (file_id, True, None)


def test_limiter_kinds_used(harness: _Harness) -> None:
    """Charge status queries as reads even though they are PUT requests."""
    drive, fake = harness.drive, harness.fake
    file_id, folder_id = drive.generate_ids(2)
    drive.about()
    drive.create_folder(file_id=folder_id, name="folder", parent_id=fake.root_id, properties={})
    drive.update_metadata(folder_id, name="renamed")
    url = drive.start_resumable(
        size=1,
        file_id=file_id,
        name="file",
        parent_id=folder_id,
        modified_ms=0,
        properties=None,
        update=False,
    )
    assert drive.resumable_status(url, 1) == 0
    drive.resumable_put(url, b"x", 0, 1)
    drive.download(file_id, BytesIO())
    assert harness.limiter.kinds == [
        "read",
        "read",
        "write",
        "write",
        "write",
        "read",
        "write",
        "read",
    ]


class _BrokenBody(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"abc"
        raise httpx.ReadError("interrupted")


def test_download_retries_stream_at_written_offset() -> None:
    """Continue a failed stream via Range without repeating bytes or callbacks."""
    ranges: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("Range"))
        if len(ranges) == 1:
            return httpx.Response(200, stream=_BrokenBody())
        return httpx.Response(206, headers={"Content-Range": "bytes 3-5/6"}, content=b"def")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        harness = _Harness(FakeDrive(), http)
        sink = BytesIO()
        deltas: list[int] = []
        harness.drive.download("file", sink, on_bytes=deltas.append)
    assert ranges == [None, "bytes=3-"]
    assert sink.getvalue() == b"abcdef"
    assert deltas == [3, 3]


def test_session_url_cannot_send_bearer_to_another_origin(harness: _Harness) -> None:
    """Reject an off-origin upload URL before building an authenticated request."""
    with pytest.raises(DriveError):
        harness.drive.resumable_status("https://example.test/upload", 1)
    assert not harness.fake.calls


@pytest.mark.parametrize("operation", ["status", "put"])
def test_resumable_rejects_invalid_range(operation: str) -> None:
    """Never extrapolate a confirmed offset from an invalid Range header."""
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(308, headers={"Range": "bytes=10-29"})
        )
    ) as http:
        drive = _Harness(FakeDrive(), http).drive
        url = FakeDrive.API_BASE + "/session"
        calls: dict[str, Callable[[], object]] = {
            "status": lambda: drive.resumable_status(url, 30),
            "put": lambda: drive.resumable_put(url, b"x", 0, 30),
        }
        with pytest.raises(DriveError):
            calls[operation]()


def test_resumable_zero_byte_put(harness: _Harness) -> None:
    """Use the wildcard range to commit an empty resumable upload."""
    drive, fake = harness.drive, harness.fake
    (file_id,) = drive.generate_ids(1)
    url = drive.start_resumable(
        size=0,
        file_id=file_id,
        name="empty",
        parent_id=fake.root_id,
        modified_ms=0,
        properties={},
        update=False,
    )
    file = drive.resumable_put(url, b"", 0, 0)
    assert not isinstance(file, int) and file.id == file_id and file.size == 0


def test_upload_session_preserves_opaque_query_with_client_defaults() -> None:
    """Keep the session capability and send only the explicitly selected fields."""
    fake = FakeDrive()
    with fake.client(params={"fields": "*", "upload_id": "ignored"}) as http:
        drive = _Harness(fake, http).drive
        (file_id,) = drive.generate_ids(1)
        url = drive.start_resumable(
            size=1,
            file_id=file_id,
            name="file",
            parent_id=fake.root_id,
            modified_ms=0,
            properties=None,
            update=False,
        )
        assert drive.resumable_status(url, 1) == 0
        result = drive.resumable_put(url, b"x", 0, 1)
        drive.download(file_id, BytesIO())
    assert not isinstance(result, int) and result.id == file_id
    assert set(fake.calls[2].query) == {"upload_id"}
    assert set(fake.calls[3].query) == {"upload_id"}
    assert set(fake.calls[4].query) == {"alt"}


def test_direct_chunk_retries_transport_but_not_http(harness: _Harness) -> None:
    """Leave retryable HTTP failures for the uploader to probe before replaying."""
    drive, fake = harness.drive, harness.fake
    (file_id,) = drive.generate_ids(1)
    url = drive.start_resumable(
        size=1,
        file_id=file_id,
        name="file",
        parent_id=fake.root_id,
        modified_ms=0,
        properties=None,
        update=False,
    )
    fake.faults.add(Fault(FaultPlan.is_write, "status", status=503))
    with pytest.raises(ServerError):
        drive.resumable_put(url, b"x", 0, 1)
    assert [call.status for call in fake.calls if call.method == "PUT"] == [503]
    assert harness.sleeps == []
    fake.faults.add(Fault(FaultPlan.is_write, "transport_error"))
    result = drive.resumable_put(url, b"x", 0, 1)
    assert not isinstance(result, int) and result.id == file_id
    assert [call.status for call in fake.calls if call.method == "PUT"] == [503, None, 200]
    assert len(harness.sleeps) == 1


def test_download_refuses_full_response_to_range_retry() -> None:
    """Reject a restarted 200 response before writing any duplicate bytes."""

    def respond(request: httpx.Request) -> httpx.Response:
        if "Range" not in request.headers:
            return httpx.Response(200, stream=_BrokenBody())
        return httpx.Response(200, content=b"abcdef")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        drive = _Harness(FakeDrive(), http).drive
        sink = BytesIO()
        with pytest.raises(DriveError):
            drive.download("file", sink)
    assert sink.getvalue() == b"abc"
