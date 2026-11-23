"""Exercise both upload protocols and inspect their persisted results."""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
import pytest

from nimbo.core.model import EMPTY_MD5, FILE_FIELDS, FOLDER_MIME, RemoteFile

from . import FakeDrive, Fault
from .test_fake_contract import FILES, connect

UPLOAD = "/upload" + FILES
CHUNK = b"a" * (256 << 10)


def multipart(
    client: httpx.Client,
    metadata: Mapping[str, object],
    data: bytes,
    *,
    file_id: str | None = None,
) -> httpx.Response:
    """Send a Drive multipart/related request with a binary media part."""
    boundary = b"fake-test-boundary"
    body = (
        b"--"
        + boundary
        + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata).encode()
        + b"\r\n--"
        + boundary
        + b"\r\nContent-Type: text/plain\r\n\r\n"
        + data
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    return client.request(
        "POST" if file_id is None else "PATCH",
        UPLOAD if file_id is None else f"{UPLOAD}/{file_id}",
        params={"uploadType": "multipart", "fields": FILE_FIELDS},
        headers={"Content-Type": f'multipart/related; boundary="{boundary.decode()}"'},
        content=body,
    )


def start(
    client: httpx.Client,
    size: int,
    *,
    file_id: str | None = None,
    fields: str = FILE_FIELDS,
    metadata: Mapping[str, object] | None = None,
) -> str:
    """Start an upload with known length and return its session URI."""
    response = client.request(
        "POST" if file_id is None else "PATCH",
        UPLOAD if file_id is None else f"{UPLOAD}/{file_id}",
        params={"uploadType": "resumable", "fields": fields},
        headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "text/plain"},
        json={"name": "blob"} if metadata is None else metadata,
    )
    assert response.status_code == 200
    return response.headers["Location"]


def put(client: httpx.Client, url: str, content_range: str, data: bytes = b"") -> httpx.Response:
    """Send a chunk or an empty status probe to the session URI."""
    return client.put(url, headers={"Content-Range": content_range}, content=data)


def test_multipart_create_and_update() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        file_id = client.get(f"{FILES}/generateIds").json()["ids"][0]
        created = multipart(
            client, {"id": file_id, "name": "one", "properties": {"a": "b"}}, b"abc\x00\xff\r\n"
        )
        assert created.status_code == 200
        assert created.json()["mimeType"] == "text/plain"
        assert fake.file(file_id).content == b"abc\x00\xff\r\n"
        updated = multipart(
            client, {"name": "two", "properties": {"d": "e"}}, b"def", file_id=file_id
        )
        updated.raise_for_status()
        assert updated.json()["id"] == file_id
        assert updated.json()["properties"] == {"a": "b", "d": "e"}
        assert fake.file(file_id).content == b"def"
        bad = client.post(UPLOAD, params={"uploadType": "multipart"}, files={"stock": b"x"})
        assert bad.status_code == 400


def test_resumable_full_flow() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        total = len(CHUNK) + 1
        url = start(client, total)
        assert url.startswith(fake.API_BASE)
        assert "upload_id=" in url
        partial = put(client, url, f"bytes 0-{len(CHUNK) - 1}/{total}", CHUNK)
        assert partial.status_code == 308
        assert partial.headers["Range"] == f"bytes=0-{len(CHUNK) - 1}"
        assert fake.snapshot(fake.root_id) == {}
        complete = put(client, url, f"bytes {len(CHUNK)}-{len(CHUNK)}/{total}", b"!")
        assert complete.status_code == 200
        file_id = complete.json()["id"]
        assert fake.file(file_id).content == CHUNK + b"!"
        update_url = start(client, 1, file_id=file_id, fields="id,size")
        assert update_url != url
        assert put(client, update_url, "bytes 0-0/1", b"b").json() == {"id": file_id, "size": "1"}


def test_resumable_status_query() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        total = len(CHUNK) + 1
        spec = f"bytes */{total}"
        url = start(client, total, fields="id,size")
        empty = put(client, url, spec)
        assert empty.status_code == 308
        assert "Range" not in empty.headers
        put(client, url, f"bytes 0-{len(CHUNK) - 1}/{total}", CHUNK)
        partial = put(client, url, spec)
        assert partial.status_code == 308
        assert partial.headers["Range"] == f"bytes=0-{len(CHUNK) - 1}"
        final = put(client, url, f"bytes {len(CHUNK)}-{len(CHUNK)}/{total}", b"b")
        complete = put(client, url, spec)
        assert complete.status_code == 200
        assert complete.json() == final.json()
        assert fake.write_count() == 3


def test_resumable_wrong_offset_returns_308() -> None:
    with connect(FakeDrive()) as client:
        total = len(CHUNK) + 1
        url = start(client, total)
        response = put(client, url, f"bytes {len(CHUNK)}-{len(CHUNK)}/{total}", b"x")
        assert response.status_code == 308
        assert "Range" not in response.headers
        part = f"bytes 0-{len(CHUNK) - 1}/{total}"
        for _ in range(2):
            response = put(client, url, part, CHUNK)
            assert response.status_code == 308
            assert response.headers["Range"] == f"bytes=0-{len(CHUNK) - 1}"


def test_non_aligned_chunk_rejected() -> None:
    with connect(FakeDrive()) as client:
        url = start(client, len(CHUNK) + 1)
        response = put(client, url, f"bytes 0-1/{len(CHUNK) + 1}", b"ab")
        assert response.status_code == 400
        assert "Range" not in put(client, url, f"bytes */{len(CHUNK) + 1}").headers
        assert put(client, url, "bytes 0-0/1", b"a").status_code == 400
        assert put(client, url, f"bytes */{len(CHUNK) + 1}", b"bad").status_code == 400


def test_zero_byte_resumable() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        url = start(client, 0)
        completed = put(client, url, "bytes */0")
        assert completed.status_code == 200
        assert completed.json()["size"] == "0"
        assert completed.json()["md5Checksum"] == EMPTY_MD5
        assert put(client, url, "bytes */0").json() == completed.json()
        assert fake.write_count() == 2


def test_expired_session_404() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        url = start(client, 1)
        fake.expire_upload_sessions()
        assert put(client, url, "bytes */1").status_code == 404
        assert put(client, f"{UPLOAD}?upload_id=unknown", "bytes */1").status_code == 404


def test_md5_and_size_reported() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        file_id = multipart(client, {}, b"abc").json()["id"]
        raw = client.get(f"{FILES}/{file_id}", params={"fields": FILE_FIELDS}).json()
        assert raw["size"] == "3"
        assert raw["md5Checksum"] == "900150983cd24fb0d6963f7d28e17f72"
        assert RemoteFile.from_api(raw).size == 3
        folder = fake.add_folder("folder")
        native = fake.add_native("native", fake.root_id)
        for item in (folder, native):
            metadata = client.get(f"{FILES}/{item}", params={"fields": FILE_FIELDS}).json()
            assert "md5Checksum" not in metadata and "size" not in metadata
        assert client.get(f"{FILES}/{folder}").json()["mimeType"] == FOLDER_MIME


def test_failed_upload_does_not_consume_id_or_daily_bytes() -> None:
    fake = FakeDrive(quota_limit=2, daily_upload_limit=2)
    with connect(fake) as client:
        file_id = client.get(f"{FILES}/generateIds").json()["ids"][0]
        metadata = {"id": file_id}
        assert multipart(client, metadata, b"abc").status_code == 403
        assert multipart(client, metadata, b"ab").status_code == 200
        assert set(fake.snapshot(fake.root_id).values()) == {(False, 2, fake.file(file_id).md5)}


def test_corruption_hook_applies_after_the_request() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        file_id = multipart(client, {}, b"before").json()["id"]
        modified = fake.file(file_id).modified_ms
        fake.set_content(file_id, b"changed")
        assert fake.file(file_id).modified_ms == modified
        assert client.get(f"{FILES}/{file_id}", params={"alt": "media"}).content == b"changed"


def test_dropped_final_response_can_be_recovered() -> None:
    fake = FakeDrive()
    with connect(fake) as client:
        url = start(client, 1)
        fake.faults.add(Fault(lambda req: req.method == "PUT", "drop_response"))
        with pytest.raises(httpx.ReadTimeout):
            put(client, url, "bytes 0-0/1", b"x")
        response = put(client, url, "bytes */1")
        assert response.status_code == 200
        assert fake.file(response.json()["id"]).content == b"x"
        assert fake.write_count() == 2


def test_concurrent_sessions_recheck_quota_at_commit() -> None:
    fake = FakeDrive(quota_limit=2)
    with connect(fake) as client:
        first, second = start(client, 2), start(client, 2)
        uploaded = put(client, first, "bytes 0-1/2", b"ab")
        uploaded.raise_for_status()
        rejected = put(client, second, "bytes 0-1/2", b"cd")
        assert rejected.status_code == 403
        assert rejected.json()["error"]["errors"][0]["reason"] == "storageQuotaExceeded"
        assert "Range" not in put(client, second, "bytes */2").headers
        client.delete(f"{FILES}/{uploaded.json()['id']}").raise_for_status()
        put(client, second, "bytes 0-1/2", b"cd").raise_for_status()
