"""Exercise real upload session persistence and verified restores."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest

from nimbo.core.drive import DriveClient
from nimbo.core.errors import Conflict, FileChanged, Md5Mismatch, ServerError, Stopped
from nimbo.core.index import Index
from nimbo.core.model import EMPTY_MD5, PART_SUFFIX, drive_ms
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.transfer import Downloader, Uploader, UploadRequest, create_session_kind
from tests.fakedrive import FakeDrive, Fault, FaultPlan, SimulatedCrash

_CHUNK = 262144
_LARGE = b"".join(bytes([n]) * _CHUNK for n in range(5)) + b"last chunk" * 10_000


class _Tokens:
    def __init__(self, fake: FakeDrive) -> None:
        self._value = fake.oauth.add_access_token("test-token")

    def token(self) -> str:
        """Return the registered test token."""
        return self._value

    def invalidate(self) -> None:
        """Fail if a test unexpectedly requires token refresh."""
        pytest.fail("unexpected token invalidation")


@dataclass
class _Harness:
    fake: FakeDrive
    drive: DriveClient
    index: Index
    index_path: Path
    root: Path
    sleeps: list[float]

    def request(self, data: bytes = _LARGE, *, update: bool = False) -> UploadRequest:
        """Write a local fixture and prepare its create or update request."""
        path = self.root / "data"
        path.write_bytes(data)
        os.utime(path, ns=(1_234_567_890, 1_234_567_890))
        file_id = (
            self.fake.add_file(path.name, self.fake.root_id, b"old")
            if update
            else self.drive.generate_ids(1)[0]
        )
        return UploadRequest(
            path.name,
            path,
            len(data),
            path.stat().st_mtime_ns,
            file_id,
            update,
            name=None if update else path.name,
            parent_id=None if update else self.fake.root_id,
            properties={"nimbo": "owner"},
        )

    def uploader(self, *, multipart_max: int = _CHUNK) -> Uploader:
        """Construct an uploader with an aligned small chunk size."""
        return Uploader(self.drive, self.index, chunk_size=_CHUNK, multipart_max=multipart_max)

    def session(self, req: UploadRequest) -> tuple[str, str] | None:
        """Get the saved record for a request."""
        assert req.update or req.parent_id is not None
        kind = "update" if req.update else create_session_kind(req.parent_id)
        return self.index.get_session(req.rel, req.size, req.mtime_ns, kind)

    def use_new_index(self) -> None:
        """Reopen the on-disk index to simulate a later process."""
        self.index.close()
        self.index = Index(self.index_path)


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[_Harness]:
    """Use FakeDrive with a real temporary index and no real sleeping."""
    fake = FakeDrive()
    sleeps: list[float] = []
    with fake.client() as http:
        drive = DriveClient(
            http,
            _Tokens(fake),
            RateLimiter(
                reads_per_second=1000,
                writes_per_second=1000,
                clock=lambda: 0.0,
                sleep=lambda _: pytest.fail("unexpected limiter wait"),
            ),
            RetryPolicy(max_attempts=3, base_delay=0),
            sleep=sleeps.append,
        )
        index_path = tmp_path / "index.sqlite"
        harness = _Harness(fake, drive, Index(index_path), index_path, tmp_path, sleeps)
        try:
            yield harness
        finally:
            harness.index.close()


def _data_put(request: httpx.Request) -> bool:
    return request.method == "PUT" and request.headers.get("Content-Range", "").startswith(
        "bytes 0-"
    )


def _final_put(request: httpx.Request) -> bool:
    value = request.headers.get("Content-Range", "")
    return request.method == "PUT" and value.endswith(f"-{len(_LARGE) - 1}/{len(_LARGE)}")


def test_small_file_multipart_create(remote: _Harness) -> None:
    """Create a small file using multipart metadata followed by exact binary data."""
    data = b"hello\x00\xff\r\n"
    req = remote.request(data)
    outcome = remote.uploader().upload(req)
    assert outcome.file.id == req.file_id and outcome.bytes_sent == len(data)
    assert outcome.file.md5 == outcome.local_md5 == hashlib.md5(data).hexdigest()
    assert outcome.file.modified_ms == drive_ms(req.mtime_ns)
    assert outcome.file.parents == (remote.fake.root_id,)
    assert outcome.file.properties == req.properties and remote.session(req) is None
    assert remote.fake.file(req.file_id).content == data
    assert remote.fake.calls[-1].query["uploadType"] == "multipart"


def test_small_file_multipart_update_keeps_id(remote: _Harness) -> None:
    """Update one file in place without changing its name or parent."""
    req = remote.request(b"new contents", update=True)
    result = remote.uploader().upload(req)
    assert result.file.id == req.file_id and result.file.name == req.path.name
    assert result.file.parents == (remote.fake.root_id,)
    assert remote.fake.file(req.file_id).content == b"new contents"
    assert [item.id for item in remote.fake.find(remote.fake.root_id, req.rel)] == [req.file_id]
    assert (remote.fake.calls[-1].method, remote.fake.calls[-1].query["uploadType"]) == (
        "PATCH",
        "multipart",
    )


@pytest.mark.parametrize("update", [False, True])
def test_large_file_resumable_create(remote: _Harness, update: bool) -> None:
    """Persist the index record before the first aligned chunk and clear it after finishing."""
    req = remote.request(update=update)
    observed_first = []

    def check_session(request: httpx.Request) -> bool:
        if _data_put(request):
            assert remote.session(req) == (req.file_id, str(request.url))
            observed_first.append(request)
        return False

    remote.fake.faults.add(Fault(check_session, "transport_error"))
    outcome = remote.uploader().upload(req)
    assert len(observed_first) == 1 and outcome.bytes_sent == len(_LARGE)
    assert outcome.file.id == req.file_id and outcome.local_md5 == hashlib.md5(_LARGE).hexdigest()
    assert remote.fake.file(req.file_id).content == _LARGE and remote.session(req) is None
    puts = [call for call in remote.fake.calls if call.method == "PUT"]
    assert [call.status for call in puts] == [308] * 5 + [200]
    initiate = next(
        call for call in remote.fake.calls if call.query.get("uploadType") == "resumable"
    )
    assert initiate.method == ("PATCH" if update else "POST")


def test_resume_after_stop_sends_only_remaining_bytes(remote: _Harness) -> None:
    """Keep the session and include its already-confirmed bytes in the full hash."""
    req = remote.request()
    partial: list[int] = []
    with pytest.raises(Stopped):
        remote.uploader().upload(req, on_bytes=partial.append, should_stop=lambda: bool(partial))
    assert partial == [_CHUNK] and remote.session(req) is not None
    assert not remote.fake.find(remote.fake.root_id, req.rel)

    resumed: list[int] = []
    outcome = remote.uploader().upload(req, on_bytes=resumed.append)
    assert outcome.bytes_sent == len(_LARGE) - _CHUNK
    assert outcome.local_md5 == hashlib.md5(_LARGE).hexdigest()
    assert sum(resumed) == len(_LARGE) and resumed[0] == _CHUNK
    assert remote.fake.file(req.file_id).content == _LARGE and remote.session(req) is None


@pytest.mark.parametrize("action", ["crash_before", "crash_after"])
def test_resume_after_simulated_crash_uses_stored_session(remote: _Harness, action: str) -> None:
    """Recover the server offset after reopening SQLite on either side of a chunk write."""
    req = remote.request()
    remote.fake.faults.add(Fault(_data_put, action))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    saved = remote.session(req)
    assert saved is not None and saved[0] == req.file_id

    remote.use_new_index()
    assert remote.session(req) == saved
    outcome = remote.uploader().upload(req)
    assert outcome.bytes_sent == len(_LARGE) - (_CHUNK if action == "crash_after" else 0)
    assert outcome.file.md5 == hashlib.md5(_LARGE).hexdigest()
    assert remote.session(req) is None


def test_expired_session_restarts(remote: _Harness) -> None:
    """Drop an expired capability and restart for the same file ID."""
    req = remote.request()
    remote.fake.faults.add(Fault(_data_put, "crash_after"))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    saved = remote.session(req)
    remote.fake.expire_upload_sessions()

    outcome = remote.uploader().upload(req)
    assert outcome.bytes_sent == req.size and outcome.file.id == req.file_id
    starts = [call for call in remote.fake.calls if call.query.get("uploadType") == "resumable"]
    chunks = [call for call in remote.fake.calls if call.method == "PUT"]
    assert saved is not None and chunks[0].query != chunks[-1].query
    assert len(starts) == 2 and any(call.status == 404 for call in chunks)
    assert remote.session(req) is None


def test_completed_session_detected_on_resume(remote: _Harness) -> None:
    """Verify a completed upload found by status without sending media again."""
    req = remote.request()
    remote.fake.faults.add(Fault(_final_put, "crash_after"))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    assert remote.session(req) is not None and remote.fake.file(req.file_id).content == _LARGE

    remote.use_new_index()
    remote.fake.calls.clear()
    result = remote.uploader().upload(req)
    assert result.bytes_sent == 0 and result.local_md5 == hashlib.md5(_LARGE).hexdigest()
    assert [(call.method, call.status) for call in remote.fake.calls] == [("PUT", 200)]
    assert remote.session(req) is None


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_create_conflict_after_completion_is_success(remote: _Harness, data: bytes) -> None:
    """A pre-generated ID already committed with matching content is success."""
    req = remote.request(data)
    remote.uploader().upload(req)
    remote.fake.expire_upload_sessions()
    remote.fake.calls.clear()
    result = remote.uploader().upload(req)
    assert result.file.id == req.file_id and result.local_md5 == hashlib.md5(data).hexdigest()
    assert [(call.method, call.status) for call in remote.fake.calls] == [
        ("POST", 409),
        ("GET", 200),
    ]
    assert [item.id for item in remote.fake.find(remote.fake.root_id, req.rel)] == [req.file_id]
    assert remote.session(req) is None


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_create_existing_with_wrong_content_switches_to_update(
    remote: _Harness, data: bytes
) -> None:
    """Repair same-size corruption found after the reserved ID returns HTTP 409."""
    req = remote.request(data)
    remote.uploader().upload(req)
    remote.fake.set_content(req.file_id, b"X" + data[1:], keep_modified=True)
    corrupted = remote.fake.file(req.file_id)
    assert (corrupted.size, corrupted.modified_ms) == (req.size, drive_ms(req.mtime_ns))

    remote.fake.calls.clear()
    deltas: list[int] = []
    result = remote.uploader().upload(req, on_bytes=deltas.append)

    assert result.file.id == req.file_id
    assert result.file.md5 == result.local_md5 == hashlib.md5(data).hexdigest()
    assert remote.fake.file(req.file_id).content == data
    assert [item.id for item in remote.fake.find(remote.fake.root_id, req.rel)] == [req.file_id]
    assert [(call.method, call.status) for call in remote.fake.calls[:3]] == [
        ("POST", 409),
        ("GET", 200),
        ("PATCH", 200),
    ]
    assert sum(deltas) == req.size
    assert remote.session(req) is None and remote.session(replace(req, update=True)) is None


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
@pytest.mark.parametrize("foreign", ["folder", "name", "parent"])
def test_existing_object_not_ours_still_conflicts(
    remote: _Harness, data: bytes, foreign: str
) -> None:
    """Leave a reserved ID with a different type, parent or name untouched."""
    req = remote.request(data)
    if foreign == "folder":
        remote.drive.create_folder(
            file_id=req.file_id, name=req.rel, parent_id=remote.fake.root_id, properties={}
        )
    elif foreign == "name":
        remote.uploader().upload(replace(req, name="another"))
    else:
        parent = remote.fake.add_folder("another")
        remote.uploader().upload(replace(req, parent_id=parent))
    existing = remote.fake.file(req.file_id)
    remote.fake.calls.clear()

    with pytest.raises(Conflict) as error:
        remote.uploader().upload(req)

    assert error.value.status == 409
    assert remote.fake.file(req.file_id) == existing
    assert [(call.method, call.status) for call in remote.fake.calls] == [
        ("POST", 409),
        ("GET", 200),
    ]


@contextmanager
def _corrupt_uploads(
    remote: _Harness, req: UploadRequest, data: bytes, *, count: int
) -> Iterator[Uploader]:
    original = remote.fake.transport()

    def corrupt_response(request: httpx.Request) -> httpx.Response:
        nonlocal count
        response = original.handle_request(request)
        if (
            count
            and response.status_code == 200
            and (_final_put(request) or request.url.params.get("uploadType") == "multipart")
        ):
            assert response.json()["id"] == req.file_id
            count -= 1
            remote.fake.set_content(req.file_id, b"X" + data[1:], keep_modified=True)
            item = remote.fake.file(req.file_id)
            return httpx.Response(200, json=item.as_api())
        return response

    with httpx.Client(transport=httpx.MockTransport(corrupt_response)) as http:
        drive = DriveClient(
            http,
            _Tokens(remote.fake),
            RateLimiter(reads_per_second=1000, writes_per_second=1000),
            RetryPolicy(max_attempts=1),
            sleep=lambda _: pytest.fail("unexpected retry"),
        )
        yield Uploader(drive, remote.index, chunk_size=_CHUNK, multipart_max=_CHUNK)


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_md5_mismatch_on_create_converges_on_same_id(remote: _Harness, data: bytes) -> None:
    """Repair corrupted bytes returned after a successful create on the same ID."""
    req = remote.request(data)
    deltas: list[int] = []
    with _corrupt_uploads(remote, req, data, count=1) as uploader:
        result = uploader.upload(req, on_bytes=deltas.append)
    assert result.file.id == req.file_id and result.bytes_sent == 2 * req.size
    assert result.file.md5 == result.local_md5 == hashlib.md5(data).hexdigest()
    assert remote.fake.file(req.file_id).content == data
    assert [item.id for item in remote.fake.find(remote.fake.root_id, req.rel)] == [req.file_id]
    assert sum(deltas) == req.size
    assert [call.method for call in remote.fake.calls if call.method in {"POST", "PATCH"}] == [
        "POST",
        "PATCH",
    ]


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_update_mismatch_retries_once(remote: _Harness, data: bytes) -> None:
    """Retry a corrupt update in place even without a preceding create."""
    req = remote.request(data, update=True)
    with _corrupt_uploads(remote, req, data, count=1) as uploader:
        result = uploader.upload(req)
    assert result.file.id == req.file_id and result.bytes_sent == 2 * req.size
    assert remote.fake.file(req.file_id).content == data
    assert [call.method for call in remote.fake.calls if call.method == "PATCH"] == ["PATCH"] * 2
    assert remote.session(req) is None


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
@pytest.mark.parametrize("update", [False, True], ids=["create", "update"])
def test_md5_mismatch_raises(remote: _Harness, data: bytes, update: bool) -> None:
    """Raise after two mismatches and keep exactly one object."""
    req = remote.request(data, update=update)
    with _corrupt_uploads(remote, req, data, count=2) as uploader, pytest.raises(Md5Mismatch):
        uploader.upload(req)
    assert [call.method for call in remote.fake.calls if call.method in {"POST", "PATCH"}] == [
        "PATCH" if update else "POST",
        "PATCH",
    ]
    assert [item.id for item in remote.fake.find(remote.fake.root_id, req.rel)] == [req.file_id]
    assert remote.session(req) is None and remote.session(replace(req, update=True)) is None


def test_resume_interrupted_create_repair(remote: _Harness) -> None:
    """Resume the update session created by a prior call with the original create request."""
    req = remote.request()
    remote.uploader().upload(req)
    remote.fake.set_content(req.file_id, b"X" + _LARGE[1:], keep_modified=True)
    remote.fake.faults.add(
        Fault(
            lambda request: _data_put(request) and request.url.path.endswith("/" + req.file_id),
            "crash_after",
        )
    )
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    repair = replace(req, update=True)
    assert remote.session(req) is None and remote.session(repair) is not None

    remote.use_new_index()
    result = remote.uploader().upload(req)
    assert result.bytes_sent == req.size - _CHUNK
    assert remote.fake.file(req.file_id).content == _LARGE
    assert remote.session(repair) is None
    assert len([call for call in remote.fake.calls if call.method == "PATCH"]) == 1


def test_create_session_with_different_parent_is_not_resumed(remote: _Harness) -> None:
    req = remote.request()
    remote.fake.faults.add(Fault(_data_put, "crash_after"))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    old_session = remote.session(req)
    assert old_session is not None
    dest = remote.fake.add_folder("New parent")
    replacement = replace(req, parent_id=dest, file_id=remote.drive.generate_ids(1)[0])
    remote.fake.calls.clear()

    result = remote.uploader().upload(replacement)

    assert result.file.parents == (dest,)
    assert result.file.id == replacement.file_id
    assert remote.session(req) == old_session and remote.session(replacement) is None
    assert not remote.fake.find(remote.fake.root_id, req.rel)
    assert all(
        call.query.get("upload_id") != httpx.URL(old_session[1]).params["upload_id"]
        for call in remote.fake.calls
    )


def test_file_changed_before_upload(remote: _Harness) -> None:
    """Refuse to issue any new request for a stale scan entry."""
    req = remote.request(b"before")
    req.path.write_bytes(b"different size")
    remote.fake.calls.clear()
    with pytest.raises(FileChanged):
        remote.uploader().upload(req)
    assert not remote.fake.calls


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_replaced_upload_source_is_not_followed_or_opened(
    remote: _Harness, replacement: str
) -> None:
    """Reject replacements ignored by the scanner even if metadata could match."""
    req = remote.request(b"data")
    clone = remote.root / "clone"
    req.path.rename(clone)
    if replacement == "symlink":
        req.path.symlink_to(clone)
    else:
        os.mkfifo(req.path)
    remote.fake.calls.clear()
    with pytest.raises(FileChanged):
        remote.uploader().upload(req)
    assert not remote.fake.calls


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_file_changed_during_upload(remote: _Harness, data: bytes) -> None:
    """Detect an mtime-only change after reading and discard any saved session."""
    req = remote.request(data)

    def mutate(request: httpx.Request) -> bool:
        if _final_put(request) or request.url.params.get("uploadType") == "multipart":
            changed_ns = req.mtime_ns + 2_000_000_000
            os.utime(req.path, ns=(changed_ns, changed_ns))
        return False

    remote.fake.faults.add(Fault(mutate, "transport_error"))
    with pytest.raises(FileChanged):
        remote.uploader().upload(req)
    assert remote.session(req) is None


@pytest.mark.parametrize("multipart_max", [0, -1], ids=["multipart", "resumable"])
def test_zero_byte_file(remote: _Harness, multipart_max: int) -> None:
    """Commit empty content with either protocol and no spurious progress."""
    req = remote.request(b"")
    deltas: list[int] = []
    outcome = remote.uploader(multipart_max=multipart_max).upload(req, on_bytes=deltas.append)
    assert (outcome.bytes_sent, outcome.file.size, outcome.local_md5) == (0, 0, EMPTY_MD5)
    assert remote.fake.file(req.file_id).content == b"" and remote.session(req) is None
    assert not deltas


@pytest.mark.parametrize("data", [b"small", _LARGE], ids=["multipart", "resumable"])
def test_on_bytes_totals_equal_size(remote: _Harness, data: bytes) -> None:
    """Emit positive deltas totaling the exact size."""
    req = remote.request(data)
    deltas: list[int] = []
    remote.uploader().upload(req, on_bytes=deltas.append)
    assert min(deltas) > 0 and sum(deltas) == req.size


@pytest.mark.parametrize(
    ("action", "status"), [("drop_response", 500), ("status", 503), ("status", 429)]
)
def test_chunk_retry_probes_status_before_replaying(
    remote: _Harness,
    action: str,
    status: int,
) -> None:
    """Use the confirmed offset after ambiguous or retryable chunk failures."""
    req = remote.request()
    ranges: list[str] = []

    def observe(request: httpx.Request) -> bool:
        if request.method == "PUT":
            ranges.append(request.headers["Content-Range"])
        return False

    remote.fake.faults.add(Fault(observe, "transport_error"))
    remote.fake.faults.add(Fault(_data_put, action, status=status))
    outcome = remote.uploader().upload(req)
    assert outcome.file.id == req.file_id and remote.fake.file(req.file_id).content == _LARGE
    assert ranges[:2] == [f"bytes 0-{_CHUNK - 1}/{req.size}", f"bytes */{req.size}"]
    assert outcome.bytes_sent == req.size + (0 if action == "drop_response" else _CHUNK)


def test_chunk_failure_budget_preserves_session(remote: _Harness) -> None:
    """Bound HTTP retries and retain the capability for a later call."""
    req = remote.request()
    remote.fake.faults.add(Fault(_data_put, "status", status=503, times=10))
    with pytest.raises(ServerError):
        remote.uploader().upload(req)
    puts = [call.status for call in remote.fake.calls if call.method == "PUT"]
    assert puts == [503, 308, 503, 308, 503]
    assert remote.session(req) is not None


def test_multipart_update_lost_response_avoids_second_revision(remote: _Harness) -> None:
    """Reconcile an ambiguous update before sending the same content again."""
    req = remote.request(b"new", update=True)
    remote.fake.faults.add(Fault(FaultPlan.is_write, "drop_response"))
    before = remote.fake.write_count()
    result = remote.uploader().upload(req)
    assert result.file.id == req.file_id and remote.fake.write_count() == before + 1
    assert result.bytes_sent == req.size


def test_foreign_session_id_is_never_resumed(remote: _Harness) -> None:
    """Do not replay capability URLs for another file ID with the same local key."""
    req = remote.request()
    remote.fake.faults.add(Fault(_data_put, "crash_after"))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    (new_id,) = remote.drive.generate_ids(1)
    result = remote.uploader().upload(replace(req, file_id=new_id))
    assert result.file.id == new_id and result.bytes_sent == req.size


def test_download_verifies_and_sets_mtime(remote: _Harness) -> None:
    """Restore exact media into created directories, then preserve Drive's mtime."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, _LARGE, modified_ms=1234)
    file = remote.drive.get(file_id)
    assert file is not None
    dest = remote.root / "restore" / "notes"
    deltas: list[int] = []
    Downloader(remote.drive).download(file, dest, on_bytes=deltas.append)
    assert dest.read_bytes() == _LARGE and sum(deltas) == len(_LARGE)
    assert dest.stat().st_mtime_ns == 1_234_000_000
    assert not dest.with_name(dest.name + PART_SUFFIX).exists()


@pytest.mark.parametrize("race", [False, True], ids=["already-exists", "appears-during-transfer"])
def test_download_never_overwrites(remote: _Harness, race: bool) -> None:
    """Keep an existing file even if another writer creates it during the download."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, b"remote")
    file = remote.drive.get(file_id)
    assert file is not None
    dest = remote.root / "notes"
    if not race:
        dest.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        Downloader(remote.drive).download(file, dest, on_bytes=lambda _: dest.write_bytes(b"keep"))
    assert dest.read_bytes() == b"keep"
    assert not dest.with_name(dest.name + PART_SUFFIX).exists()


def test_download_md5_mismatch_cleans_part(remote: _Harness) -> None:
    """Remove the temp file if Drive bytes have changed after the metadata lookup."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, b"good")
    file = remote.drive.get(file_id)
    assert file is not None
    remote.fake.set_content(file_id, b"evil")
    dest = remote.root / "notes"
    with pytest.raises(Md5Mismatch):
        Downloader(remote.drive).download(file, dest)
    assert not dest.exists() and not dest.with_name(dest.name + PART_SUFFIX).exists()


def test_download_callback_failure_cleans_part(remote: _Harness) -> None:
    """Clean an owned temp file even when an interrupt aborts the stream."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, b"remote")
    file = remote.drive.get(file_id)
    assert file is not None
    dest = remote.root / "notes"

    def interrupt(_: int) -> None:
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        Downloader(remote.drive).download(file, dest, on_bytes=interrupt)
    assert not dest.exists() and not dest.with_name(dest.name + PART_SUFFIX).exists()


def test_expired_session_after_update_does_not_write_new_revision(remote: _Harness) -> None:
    """Check committed update content when a lost completion's capability has expired."""
    req = remote.request(update=True)
    remote.fake.faults.add(Fault(_final_put, "crash_after"))
    with pytest.raises(SimulatedCrash):
        remote.uploader().upload(req)
    assert remote.session(req) is not None
    before = remote.fake.write_count()
    remote.fake.expire_upload_sessions()
    result = remote.uploader().upload(req)
    assert result.file.id == req.file_id and result.bytes_sent == 0
    assert remote.fake.write_count() == before and remote.session(req) is None


def test_session_expires_between_chunks(remote: _Harness) -> None:
    """Restart once when a live session expires without double-counting progress."""
    req = remote.request()
    expired = False
    deltas: list[int] = []

    def on_bytes(delta: int) -> None:
        nonlocal expired
        deltas.append(delta)
        if not expired:
            remote.fake.expire_upload_sessions()
            expired = True

    result = remote.uploader().upload(req, on_bytes=on_bytes)
    assert remote.fake.file(req.file_id).content == _LARGE
    assert result.bytes_sent == req.size + 2 * _CHUNK and sum(deltas) == req.size
    assert remote.session(req) is None


def test_download_size_mismatch_cleans_part(remote: _Harness) -> None:
    """Reject a size mismatch even when the downloaded digest is correct."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, b"data")
    file = remote.drive.get(file_id)
    assert file is not None
    dest = remote.root / "notes"
    with pytest.raises(Md5Mismatch):
        Downloader(remote.drive).download(replace(file, size=5), dest)
    assert not dest.exists() and not dest.with_name(dest.name + PART_SUFFIX).exists()


def test_download_does_not_remove_another_transfers_part(remote: _Harness) -> None:
    """Keep an existing staging file owned by another restore."""
    file_id = remote.fake.add_file("notes", remote.fake.root_id, b"remote")
    file = remote.drive.get(file_id)
    assert file is not None
    dest = remote.root / "notes"
    part = dest.with_name(dest.name + PART_SUFFIX)
    part.write_bytes(b"staged")
    with pytest.raises(FileExistsError):
        Downloader(remote.drive).download(file, dest)
    assert not dest.exists() and part.read_bytes() == b"staged"
