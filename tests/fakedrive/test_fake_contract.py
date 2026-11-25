"""Drive metadata, queries, validation and limits through the HTTP transport."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import pytest

from nimbo.core.model import FILE_FIELDS, FOLDER_MIME, RemoteFile

from . import FakeDrive, Fault, FaultPlan, SimulatedCrash

FILES = "/drive/v3/files"
ABOUT = "/drive/v3/about"
AUTH = {"Authorization": "Bearer test-token"}


@dataclass
class Clock:
    """Mutable test clock shared by Drive and OAuth tests."""

    seconds: float = 1_800_000_000

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        """Move to a deterministic instant without waiting."""
        self.seconds += seconds


def connect(fake: FakeDrive) -> httpx.Client:
    """Register a test token and open a client with its Authorization header."""
    fake.oauth.add_access_token("test-token")
    return fake.client(headers=AUTH)


@pytest.fixture
def drive() -> Iterator[tuple[FakeDrive, httpx.Client]]:
    fake = FakeDrive()
    with connect(fake) as client:
        yield fake, client


def test_requires_bearer_token() -> None:
    clock = Clock()
    fake = FakeDrive(clock=clock)
    fake.oauth.add_access_token("temporary", expires_in=5)
    with fake.client() as client:
        for headers in ({}, {"Authorization": "Basic temporary"}, AUTH):
            response = client.get(ABOUT, headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["errors"][0]["reason"] == "authError"
        header = {"Authorization": "Bearer temporary"}
        assert client.get(ABOUT, headers=header).status_code == 200
        clock.advance(5)
        assert client.get(ABOUT, headers=header).status_code == 401


def test_fields_filtering_default_and_nested(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    file_id = fake.add_file("memo", fake.root_id, b"a", modified_ms=123)
    expected = {
        "kind": "drive#file",
        "id": file_id,
        "name": "memo",
        "mimeType": "application/octet-stream",
    }
    assert client.get(f"{FILES}/{file_id}").json() == expected
    assert client.get(FILES).json() == {"kind": "drive#fileList", "files": [expected]}
    assert client.get(FILES, params={"fields": "files(id,modifiedTime,size)"}).json() == {
        "files": [{"id": file_id, "modifiedTime": "1970-01-01T00:00:00.123Z", "size": "1"}]
    }
    about = client.get(ABOUT, params={"fields": "user/emailAddress,storageQuota(usage)"}).json()
    assert set(about["user"]) == {"emailAddress"}
    assert about["storageQuota"] == {"usage": "1"}
    full = client.get(f"{FILES}/{file_id}", params={"fields": "*"}).json()
    assert RemoteFile.from_api(full).modified_ms == 123
    assert RemoteFile.from_api(full).size == 1
    assert client.get(FILES, params={"fields": "files(*)"}).json() == {"files": [full]}
    for fields in ("files(id", "files(unknownField)", "files(id),", "files()"):
        assert client.get(FILES, params={"fields": fields}).status_code == 400


def test_list_q_parents_or_grouping_and_pagination(
    drive: tuple[FakeDrive, httpx.Client],
) -> None:
    fake, client = drive
    left, right = fake.add_folder("left"), fake.add_folder("right")
    name = "O'Brien\\draft"
    first = fake.add_file(name, left, b"x")
    second = fake.add_file(name, right, b"y")
    ignored = fake.add_file(name, left, b"z")
    fake.add_file("other", left, b"q")
    client.patch(f"{FILES}/{ignored}", json={"trashed": True}).raise_for_status()
    params: dict[str, str | int] = {
        "q": f"('{left}' in parents or '{right}' in parents) and trashed = false "
        r"and name = 'O\'Brien\\draft' and mimeType != 'application/vnd.google-apps.folder'",
        "pageSize": 1,
        "fields": "nextPageToken,files(id)",
    }
    page = client.get(FILES, params=params).json()
    assert page["files"] == [{"id": first}]
    assert not page["nextPageToken"].isdigit()
    params["pageToken"] = page["nextPageToken"]
    assert client.get(FILES, params=params).json() == {"files": [{"id": second}]}
    assert client.get(FILES, params={**params, "pageToken": "1"}).status_code == 400
    grouped = params["q"]
    assert isinstance(grouped, str)
    ungrouped = grouped.replace("(", "").replace(")", "")
    assert client.get(FILES, params={"q": ungrouped, "fields": "files(id)"}).json() == {
        "files": [{"id": first}, {"id": second}]
    }
    for size in (0, 1001, "invalid"):
        assert client.get(FILES, params={"pageSize": size}).status_code == 400


@pytest.mark.parametrize(
    "query",
    [
        "name contains 'x'",
        "trashed != false",
        "'root' in parents or name = 'x'",
        "mimeType = 'x' and",
        r"name = 'invalid\escape'",
        "trashed = false )",
    ],
)
def test_list_rejects_unsupported_q(drive: tuple[FakeDrive, httpx.Client], query: str) -> None:
    response = drive[1].get(FILES, params={"q": query})
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["reason"] == "invalid"


def test_generate_ids_and_create_with_id(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    generated = client.get(
        f"{FILES}/generateIds", params={"count": 3, "space": "drive", "type": "files"}
    ).json()
    ids = generated["ids"]
    assert len(set(ids)) == 3
    assert generated == {"ids": ids, "space": "drive", "kind": "drive#generatedIds"}
    assert fake.write_count() == 0
    response = client.post(FILES, json={"id": ids[0], "name": "folder", "mimeType": FOLDER_MIME})
    assert response.status_code == 200
    assert response.json()["id"] == ids[0]
    assert fake.find(fake.root_id, "folder")[0].id == ids[0]
    for count in (0, 1001, "garbage"):
        assert client.get(f"{FILES}/generateIds", params={"count": count}).status_code == 400


def test_create_with_reused_id_conflicts(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    file_id = client.get(f"{FILES}/generateIds").json()["ids"][0]
    client.post(FILES, json={"id": file_id, "name": "first"}).raise_for_status()
    response = client.post(FILES, json={"id": file_id, "name": "second"})
    assert response.status_code == 409
    assert response.json()["error"]["errors"][0]["reason"] == "fileIdInUse"
    assert fake.file(file_id).name == "first"


def test_create_with_unknown_id_rejected(drive: tuple[FakeDrive, httpx.Client]) -> None:
    response = drive[1].post(FILES, json={"id": "unallocated", "name": "x"})
    assert response.status_code == 400


def test_parent_missing_404(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    binary = fake.add_file("binary", fake.root_id, b"x")
    trashed = fake.add_folder("trashed")
    client.patch(f"{FILES}/{trashed}", json={"trashed": True}).raise_for_status()
    for parent in ("missing", binary, trashed):
        response = client.post(FILES, json={"name": "x", "parents": [parent]})
        assert response.status_code == 404
        assert response.json()["error"]["message"] == f"File not found: {parent}."
        assert response.json()["error"]["errors"][0]["reason"] == "notFound"


def test_device_folder_rejects_direct_children(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    device = fake.add_folder("Device", device=True)
    existing = fake.add_folder("Existing", device)
    response = client.post(FILES, json={"name": "x", "parents": [device]})
    assert response.status_code == 400
    assert response.json()["error"]["errors"][0]["reason"] == "badRequest"
    for _ in range(2):
        client.post(FILES, json={"name": "x", "parents": [existing]}).raise_for_status()
    assert len(fake.find(existing, "x")) == 2


def test_patch_metadata_and_parents(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    parent = fake.add_folder("old")
    target = fake.add_folder("new")
    original = {"keep": "yes", "delete": "yes", "change": "old"}
    file_id = fake.add_file("memo", parent, b"a", properties=original)
    response = client.patch(
        f"{FILES}/{file_id}",
        params={"addParents": target, "removeParents": parent, "fields": FILE_FIELDS},
        json={
            "name": "renamed",
            "modifiedTime": "2026-11-23T20:03:15.123Z",
            "properties": {"delete": None, "change": "new"},
        },
    )
    response.raise_for_status()
    file = RemoteFile.from_api(response.json())
    assert (file.name, file.parents, file.properties) == (
        "renamed",
        (target,),
        {"keep": "yes", "change": "new"},
    )
    assert response.json()["modifiedTime"] == "2026-11-23T20:03:15.123Z"
    assert fake.find(parent, "memo") == []
    bad = client.patch(f"{FILES}/{file_id}", params={"addParents": parent}, json={"name": "oops"})
    assert bad.status_code == 400
    assert fake.file(file_id).name == "renamed"


def test_move_cycle_rejected(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    parent = fake.add_folder("parent")
    child = fake.add_folder("child", parent)
    for destination in (child, parent):
        response = client.patch(
            f"{FILES}/{parent}",
            params={"addParents": destination, "removeParents": fake.root_id},
            json={"name": "bad"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["errors"][0]["reason"] == "invalidParent"
    assert fake.file(parent).name == "parent"


def test_trash_folder_implicit_descendants_and_untrash(
    drive: tuple[FakeDrive, httpx.Client],
) -> None:
    fake, client = drive
    parent = fake.add_folder("parent")
    child = fake.add_folder("child", parent)
    implicit = fake.add_file("implicit", child, b"abc")
    explicit = fake.add_file("explicit", child, b"de")
    client.patch(f"{FILES}/{explicit}", json={"trashed": True}).raise_for_status()
    client.patch(f"{FILES}/{parent}", json={"trashed": True}).raise_for_status()
    for file_id, explicitly_trashed in (
        (parent, True),
        (child, False),
        (implicit, False),
        (explicit, True),
    ):
        fields = {"fields": "trashed,explicitlyTrashed"}
        assert client.get(f"{FILES}/{file_id}", params=fields).json() == {
            "trashed": True,
            "explicitlyTrashed": explicitly_trashed,
        }
    assert fake.snapshot(fake.root_id) == {}
    usage = client.get(ABOUT, params={"fields": "storageQuota"}).json()["storageQuota"]
    assert usage["usage"] == usage["usageInDrive"] == usage["usageInDriveTrash"] == "5"
    client.patch(f"{FILES}/{parent}", json={"trashed": False}).raise_for_status()
    assert not fake.file(implicit).trashed
    assert fake.file(explicit).trashed


def test_quota_exceeded() -> None:
    fake = FakeDrive(quota_limit=3)
    existing = fake.add_file("binary", fake.root_id, b"ab")
    with connect(fake) as client:
        headers = {
            "X-Upload-Content-Length": "2",
            "X-Upload-Content-Type": "application/octet-stream",
        }
        response = client.post(
            "/upload" + FILES, params={"uploadType": "resumable"}, json={}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["errors"][0]["reason"] == "storageQuotaExceeded"
        headers["X-Upload-Content-Length"] = "3"
        assert (
            client.patch(
                f"/upload{FILES}/{existing}",
                params={"uploadType": "resumable"},
                json={},
                headers=headers,
            ).status_code
            == 200
        )
    with connect(FakeDrive(quota_limit=None)) as client:
        assert "limit" not in client.get(ABOUT).json()["storageQuota"]


def test_daily_upload_limit() -> None:
    clock = Clock(seconds=86_399)
    fake = FakeDrive(clock=clock, daily_upload_limit=2)
    with connect(fake) as client:
        params = {"uploadType": "resumable"}
        headers = {
            "X-Upload-Content-Length": "2",
            "X-Upload-Content-Type": "application/octet-stream",
        }
        start = client.post("/upload" + FILES, params=params, json={}, headers=headers)
        assert start.status_code == 200
        client.put(
            start.headers["Location"], headers={"Content-Range": "bytes 0-1/2"}, content=b"ab"
        ).raise_for_status()
        response = client.post("/upload" + FILES, params=params, json={}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["errors"][0]["reason"] == "uploadLimitExceeded"
        clock.advance(1)
        assert (
            client.post("/upload" + FILES, params=params, json={}, headers=headers).status_code
            == 200
        )


def test_write_rate_limit() -> None:
    clock = Clock(seconds=0)
    fake = FakeDrive(clock=clock, writes_per_second=2)
    with connect(fake) as client:
        client.post(FILES, json={"name": "first"}).raise_for_status()
        clock.advance(0.75)
        client.post(FILES, json={"name": "second"}).raise_for_status()
        response = client.post(FILES, json={"name": "third"})
        assert response.status_code == 403
        assert response.json()["error"]["errors"][0]["reason"] == "userRateLimitExceeded"
        clock.advance(0.25)
        client.post(FILES, json={"name": "third"}).raise_for_status()
        assert fake.write_count() == 3


def test_error_shape(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    fake.faults.add(Fault(lambda _: True, "status", status=403, reason="userRateLimitExceeded"))
    response = client.get(ABOUT)
    assert response.status_code == 403
    error = response.json()["error"]
    assert error == {
        "code": 403,
        "message": error["message"],
        "errors": [
            {
                "domain": "usageLimits",
                "reason": "userRateLimitExceeded",
                "message": error["message"],
            }
        ],
        "status": "PERMISSION_DENIED",
    }
    assert client.get("/unknown").status_code == 404


def test_download_and_range(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    file_id = fake.add_file("binary", fake.root_id, b"abcdef", mime_type="text/plain")
    path = f"{FILES}/{file_id}"
    params = {"alt": "media"}
    response = client.get(path, params=params)
    assert response.content == b"abcdef"
    assert response.headers["Content-Type"] == "text/plain"
    for byte_range, expected, content_range in (
        ("bytes=1-3", b"bcd", "bytes 1-3/6"),
        ("bytes=4-", b"ef", "bytes 4-5/6"),
        ("bytes=-2", b"ef", "bytes 4-5/6"),
    ):
        response = client.get(path, params=params, headers={"Range": byte_range})
        assert response.status_code == 206
        assert response.content == expected
        assert response.headers["Content-Range"] == content_range
    response = client.get(path, params=params, headers={"Range": "bytes=6-9"})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */6"


def test_faults_apply_and_record_in_registration_order(
    drive: tuple[FakeDrive, httpx.Client],
) -> None:
    fake, client = drive
    fake.faults.add(Fault(FaultPlan.is_write, "drop_response", skip=1))
    fake.faults.add(Fault(FaultPlan.is_write, "transport_error"))
    fake.faults.add(Fault(FaultPlan.is_write, "crash_before"))
    fake.faults.add(Fault(FaultPlan.is_write, "crash_after"))
    before = fake.now()
    fake.faults.add(Fault(FaultPlan.is_write, "delay", seconds=10))
    for name, error in (
        ("disconnected", httpx.ConnectError),
        ("dropped", httpx.ReadTimeout),
        ("crashed-before", SimulatedCrash),
        ("crashed-after", SimulatedCrash),
    ):
        with pytest.raises(error):
            client.post(FILES, json={"name": name})
    client.post(FILES, json={"name": "delayed"}).raise_for_status()
    assert fake.now() >= before + 10
    assert set(fake.snapshot(fake.root_id)) == {"dropped", "crashed-after", "delayed"}
    assert fake.write_count() == 3
    assert [record.status for record in fake.calls] == [None, 200, None, 200, 200]
    assert fake.calls[-1].method == "POST"
    assert fake.calls[-1].path == FILES


def test_concurrent_create_uses_a_generated_id_once(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    file_id = client.get(f"{FILES}/generateIds").json()["ids"][0]

    def create(index: int) -> int:
        return client.post(FILES, json={"id": file_id, "name": str(index)}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(create, range(16))) == [200] + [409] * 15
    assert fake.write_count() == 1


def test_invalid_metadata_cannot_partially_apply(drive: tuple[FakeDrive, httpx.Client]) -> None:
    fake, client = drive
    for payload in (b'{"name":"\\ud800"}', b'{"mimeType":"text/plain\\r\\nBad: header"}', b"[]"):
        response = client.post(FILES, content=payload, headers={"Content-Type": "application/json"})
        assert response.status_code == 400
    assert fake.snapshot(fake.root_id) == {}
    assert fake.write_count() == 0
