"""Exercise the executor against Drive, real files, and a durable journal."""

from __future__ import annotations

import errno
import json
import os
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
from typing import Any, Literal

import httpx
import pytest

from nimbo.core.drive import DriveClient
from nimbo.core.errors import AuthError
from nimbo.core.executor import Executor
from nimbo.core.filters import Matcher
from nimbo.core.index import META_FORCE_FULL, Index
from nimbo.core.model import (
    OWNER_PROPERTY,
    LocalEntry,
    Op,
    OpKind,
    OpState,
    RemoteFile,
    RunResult,
    drive_ms,
)
from nimbo.core.planner import PlannerOptions, make_plan
from nimbo.core.progress import ProgressTracker, read_progress
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.remote_sync import RemoteIndexer
from nimbo.core.transfer import Uploader, create_session_kind
from nimbo.core.tree import RemoteTree
from tests.fakedrive import FakeDrive, Fault, FaultPlan, SimulatedCrash
from tests.system.helpers import make_mirror

_CHUNK = 262144
_OWNER = "mirror-owner"
_WHEN = "2026-01-01T00:00:00Z"
_MTIME = 1_772_000_000_123_456_789


class _Tokens:
    def __init__(self, fake: FakeDrive) -> None:
        self.error: AuthError | None = None
        self.value = fake.oauth.add_access_token("test-token")
        self.invalidations = 0

    def token(self) -> str:
        """Return the fake token or simulate a provider that cannot refresh."""
        if self.error is not None:
            raise self.error
        return self.value

    def invalidate(self) -> None:
        """Record a refresh attempt after a 401."""
        self.invalidations += 1


@dataclass
class _Harness:
    fake: FakeDrive
    http: httpx.Client
    tokens: _Tokens
    drive: DriveClient
    index: Index
    db_path: Path
    root: Path
    progress_path: Path
    responses: list[Callable[[httpx.Request, httpx.Response], httpx.Response]]
    stop: Event

    def listing(self) -> RemoteTree:
        """Read a fresh, recursive listing using the real Drive client."""
        folders = [self.fake.root_id]
        items: list[RemoteFile] = []
        for folder in folders:
            for item in self.drive.list_children([folder]):
                items.append(item)
                if item.is_folder:
                    folders.append(item.id)
        return RemoteTree(self.fake.root_id, items)

    def persist(self, ops: Sequence[Op]) -> tuple[int, ProgressTracker]:
        """Seed the mirror, then durably save the plan for the run."""
        self.index.replace_remote(item for _, item in self.listing().walk())
        run = self.index.start_run("test", _WHEN)
        self.index.save_ops(run, ops)
        return run, ProgressTracker(self.progress_path, run, min_interval=3600)

    def executor(
        self, tracker: ProgressTracker, *, parallel: int = 1, md5_retries: int = 2, **kwargs: Any
    ) -> Executor:
        """Use real transfer code with a small resumable chunk size."""
        return Executor(
            drive=self.drive,
            uploader=Uploader(self.drive, self.index, chunk_size=_CHUNK, multipart_max=_CHUNK),
            index=self.index,
            source_root=self.root,
            root_uuid=_OWNER,
            tracker=tracker,
            stop=self.stop,
            parallel=parallel,
            md5_retries=md5_retries,
            **kwargs,
        )

    def mkdir(self, rel: str, *, parent: str | None = None) -> Op:
        """Allocate a folder create ID before execution."""
        return Op(
            OpKind.MKDIR,
            rel,
            "new",
            file_id=self.drive.generate_ids(1)[0],
            name=Path(rel).name,
            parent_id=parent or self.fake.root_id,
        )

    def upload(
        self,
        rel: str,
        content: bytes = b"data",
        *,
        parent: str | None = None,
        disk_rel: str | None = None,
        update: str | None = None,
    ) -> Op:
        """Write a scanned file and prepare its create or update operation."""
        path = self.root / (disk_rel or rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, ns=(_MTIME, _MTIME))
        return Op(
            OpKind.UPDATE if update else OpKind.UPLOAD,
            rel,
            "changed" if update else "new",
            file_id=update or self.drive.generate_ids(1)[0],
            name=Path(rel).name,
            parent_id=parent or self.fake.root_id,
            disk_rel=disk_rel,
            size=len(content),
            mtime_ns=path.stat().st_mtime_ns,
        )

    def states(self, run: int) -> list[tuple[OpState, str | None]]:
        """Read the committed states and errors in persisted order."""
        return [(state, code) for _, state, code in self.index.op_states(run)]

    def check_mirror(self) -> None:
        """Compare actual indexed metadata and paths with a fresh remote listing."""
        assert list(self.index.load_tree(self.fake.root_id).walk()) == list(self.listing().walk())

    def reopen(self) -> None:
        """Close the writer and open a new connection to the same journal."""
        self.index.close()
        self.index = Index(self.db_path)


@pytest.fixture
def remote(tmp_path: Path, source_root: Path, request: pytest.FixtureRequest) -> Iterator[_Harness]:
    """Provide a configurable FakeDrive, with bounded retries and no real sleeps."""
    fake = FakeDrive(**getattr(request, "param", {}))
    transport = fake.transport()
    responses: list[Callable[[httpx.Request, httpx.Response], httpx.Response]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        response = transport.handle_request(req)
        for transform in responses:
            response = transform(req, response)
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        tokens = _Tokens(fake)
        drive = DriveClient(
            http,
            tokens,
            RateLimiter(
                reads_per_second=1000,
                writes_per_second=1000,
                clock=lambda: 0.0,
                sleep=lambda _: pytest.fail("unexpected limiter wait"),
            ),
            RetryPolicy(max_attempts=3, base_delay=0),
            sleep=lambda _: None,
        )
        db_path = tmp_path / "index.sqlite"
        harness = _Harness(
            fake,
            http,
            tokens,
            drive,
            Index(db_path),
            db_path,
            source_root,
            tmp_path / "progress.json",
            responses,
            Event(),
        )
        try:
            yield harness
        finally:
            harness.index.close()


def _id(op: Op) -> str:
    assert op.file_id is not None
    return op.file_id


def _metadata(file_id: str) -> Callable[[httpx.Request], bool]:
    return lambda req: req.method == "PATCH" and req.url.path == f"/drive/v3/files/{file_id}"


def _upload(req: httpx.Request) -> bool:
    return req.url.path.startswith("/upload/") and req.method in {"POST", "PATCH"}


def _data_put(req: httpx.Request) -> bool:
    return req.method == "PUT" and not req.headers["Content-Range"].startswith("bytes */")


def _trash(rel: str, file_id: str, *, affected_files: int = 1) -> Op:
    return Op(OpKind.TRASH, rel, "deleted-locally", file_id=file_id, affected_files=affected_files)


def _all_ops(remote: _Harness) -> list[Op]:
    root = remote.fake.root_id
    folder = remote.mkdir("docs")
    upload = remote.upload(
        "docs/café", b"new\x00data", parent=_id(folder), disk_rel="docs/cafe\u0301"
    )
    update = remote.upload(
        "update", b"replacement", update=remote.fake.add_file("update", root, b"old")
    )
    touch = Op(
        OpKind.TOUCH,
        "touch",
        "metadata",
        file_id=remote.fake.add_file("touch", root, b"same", modified_ms=1000),
        mtime_ns=_MTIME,
    )
    adopt = Op(
        OpKind.ADOPT,
        "adopt",
        "adopted",
        file_id=remote.fake.add_file("adopt", root, b"same", modified_ms=2000),
    )
    move = Op(
        OpKind.MOVE,
        "docs/moved",
        "renamed",
        file_id=remote.fake.add_file("original", root, b"same"),
        parent_id=_id(folder),
        src_parent_id=root,
        src_rel="original",
        name="moved",
        mtime_ns=_MTIME,
    )
    trash = _trash("deleted", remote.fake.add_file("deleted", root, b"old"))
    return [folder, move, upload, update, touch, adopt, trash]


def test_each_op_kind_against_fake(remote: _Harness) -> None:
    """Apply all operation kinds, including an exact non-NFC local disk path."""
    ops = _all_ops(remote)
    run, tracker = remote.persist(ops)
    report = remote.executor(tracker, parallel=3).execute(run, ops)

    assert report.done == Counter(dict.fromkeys(OpKind, 1))
    assert report.failed == [] and report.stop_reason is None
    folder, move, upload, update, touch, adopt, trash = ops
    assert report.created_ids == [_id(folder), _id(upload)]
    assert report.uploaded_bytes == upload.size + update.size
    assert remote.states(run) == [(OpState.DONE, None)] * len(ops)
    assert remote.fake.file(_id(upload)).content == b"new\x00data"
    assert remote.fake.file(_id(update)).content == b"replacement"
    moved = remote.fake.file(_id(move))
    assert (moved.parents, moved.name) == ((_id(folder),), "moved")
    for op in (move, upload, update, touch, adopt):
        assert remote.fake.file(_id(op)).modified_ms == drive_ms(op.mtime_ns)
    assert remote.fake.file(_id(trash)).trashed is True
    remote.check_mirror()


def _metadata_bodies(remote: _Harness, file_id: str) -> list[dict[str, object]]:
    bodies: list[dict[str, object]] = []
    matches = _metadata(file_id)

    def record(request: httpx.Request) -> None:
        if matches(request):
            bodies.append(json.loads(request.content))

    remote.http.event_hooks["request"].append(record)
    return bodies


def test_epoch_dated_file_touch_sets_zero(remote: _Harness) -> None:
    """Send the Unix epoch in the patch body and store it in the mirror."""
    file_id = remote.fake.add_file("file", remote.fake.root_id, b"data", modified_ms=42)
    op = Op(OpKind.TOUCH, "file", "metadata", file_id=file_id, mtime_ns=0)
    run, tracker = remote.persist([op])
    bodies = _metadata_bodies(remote, file_id)

    report = remote.executor(tracker).execute(run, [op])

    assert report.done == Counter({OpKind.TOUCH: 1})
    assert [body["modifiedTime"] for body in bodies] == ["1970-01-01T00:00:00.000Z"]
    assert remote.fake.file(file_id).modified_ms == 0
    remote.check_mirror()


@pytest.mark.parametrize(("indexed", "mtime_ns"), [(True, _MTIME), (True, -1), (False, 0)])
def test_folder_move_never_sends_modified_time(
    remote: _Harness, indexed: bool, mtime_ns: int
) -> None:
    """Omit the timestamp for indexed folders and apply the zero fallback."""
    root = remote.fake.root_id
    file_id = remote.fake.add_folder("original")
    original_ms = remote.fake.file(file_id).modified_ms
    op = Op(
        OpKind.MOVE,
        "renamed",
        "renamed",
        file_id=file_id,
        parent_id=root,
        src_parent_id=root,
        name="renamed",
        mtime_ns=mtime_ns,
    )
    run, tracker = remote.persist([op])
    if not indexed:
        remote.index.delete_remote([file_id])
    bodies = _metadata_bodies(remote, file_id)

    report = remote.executor(tracker).execute(run, [op])

    assert report.done == Counter({OpKind.MOVE: 1})
    (body,) = bodies
    assert "modifiedTime" not in body
    item = remote.fake.file(file_id)
    assert (item.name, item.modified_ms) == ("renamed", original_ms)
    remote.check_mirror()


@pytest.mark.parametrize(
    ("indexed", "mtime_ns", "expected"),
    [
        (True, 0, "1970-01-01T00:00:00.000Z"),
        (True, -1, "1970-01-01T00:00:00.000Z"),
        (True, 1_234_567_890, "1970-01-01T00:00:01.234Z"),
        (False, -1, "1970-01-01T00:00:00.000Z"),
    ],
)
def test_file_move_always_sends_modified_time(
    remote: _Harness, indexed: bool, mtime_ns: int, expected: str
) -> None:
    """Send the converted timestamp for indexed files and the nonzero fallback."""
    root = remote.fake.root_id
    file_id = remote.fake.add_file("original", root, b"data", modified_ms=42)
    op = Op(
        OpKind.MOVE,
        "renamed",
        "renamed",
        file_id=file_id,
        parent_id=root,
        src_parent_id=root,
        name="renamed",
        mtime_ns=mtime_ns,
    )
    run, tracker = remote.persist([op])
    if not indexed:
        remote.index.delete_remote([file_id])
    bodies = _metadata_bodies(remote, file_id)

    report = remote.executor(tracker).execute(run, [op])

    assert report.done == Counter({OpKind.MOVE: 1})
    assert [body["modifiedTime"] for body in bodies] == [expected]
    item = remote.fake.file(file_id)
    assert (item.name, item.modified_ms) == ("renamed", drive_ms(mtime_ns))
    remote.check_mirror()


def test_phase_order_and_parent_before_child(remote: _Harness) -> None:
    """Drain each phase before starting the next, preserving folder and move order."""
    folder, move, upload, _, touch, _, trash = _all_ops(remote)
    child = remote.mkdir("docs/nested", parent=_id(folder))
    nested_upload = remote.upload("docs/nested/file", parent=_id(child))
    other = remote.fake.add_file("other", remote.fake.root_id, b"data")
    rename = Op(
        OpKind.MOVE,
        "renamed",
        "renamed",
        file_id=other,
        name="renamed",
        src_parent_id=remote.fake.root_id,
        parent_id=remote.fake.root_id,
    )
    ops = [trash, move, upload, touch, folder, child, nested_upload, rename]
    run, tracker = remote.persist(ops)
    seen: list[tuple[str, str, str | None]] = []

    def record(req: httpx.Request) -> None:
        if FaultPlan.is_write(req):
            seen.append((req.method, req.url.path, req.url.params.get("addParents")))
            if _metadata(other)(req):
                assert "removeParents" not in req.url.params

    remote.http.event_hooks["request"].append(record)
    report = remote.executor(tracker).execute(run, ops)
    assert report.failed == []
    assert seen == [
        ("POST", "/drive/v3/files", None),
        ("POST", "/drive/v3/files", None),
        ("PATCH", f"/drive/v3/files/{_id(move)}", _id(folder)),
        ("PATCH", f"/drive/v3/files/{other}", None),
        ("POST", "/upload/drive/v3/files", None),
        ("PATCH", f"/drive/v3/files/{_id(touch)}", None),
        ("POST", "/upload/drive/v3/files", None),
        ("PATCH", f"/drive/v3/files/{_id(trash)}", None),
    ]
    assert report.created_ids == [_id(folder), _id(child), _id(upload), _id(nested_upload)]
    remote.check_mirror()


def test_parallel_uploads_bounded(remote: _Harness) -> None:
    """Keep the content pool busy with exactly two concurrent uploads at peak."""
    ops = [remote.upload(f"file-{i}") for i in range(6)]
    run, tracker = remote.persist(ops)
    lock = Lock()
    barrier = Barrier(2, timeout=5)
    active = peak = 0

    def start(req: httpx.Request) -> None:
        nonlocal active, peak
        if _upload(req):
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()

    def end(response: httpx.Response) -> None:
        nonlocal active
        if _upload(response.request):
            with lock:
                active -= 1

    remote.http.event_hooks["request"].append(start)
    remote.http.event_hooks["response"].append(end)
    report = remote.executor(tracker, parallel=2).execute(run, ops)
    assert report.done == Counter({OpKind.UPLOAD: len(ops)})
    assert peak == 2 and active == 0
    remote.check_mirror()


def test_ownership_properties_set(remote: _Harness) -> None:
    """Write ownership on every non-trash kind without modifying the trashed item."""
    ops = _all_ops(remote)
    run, tracker = remote.persist(ops)
    remote.executor(tracker).execute(run, ops)

    for op in ops:
        properties = remote.fake.file(_id(op)).properties
        assert properties.get(OWNER_PROPERTY) == (None if op.kind is OpKind.TRASH else _OWNER)


def test_index_updated_after_each_op(remote: _Harness) -> None:
    """Observe prior completions and the next durable INFLIGHT row mid-run."""
    ops = _all_ops(remote)
    run, tracker = remote.persist(ops)
    start_paths: list[set[str]] = []

    def inspect(req: httpx.Request) -> None:
        if FaultPlan.is_write(req):
            reader = Index.open_readonly(remote.db_path)
            try:
                states = [state for _, state, _ in reader.op_states(run)]
                assert states == (
                    [OpState.DONE] * len(start_paths)
                    + [OpState.INFLIGHT]
                    + [OpState.PENDING] * (len(ops) - len(start_paths) - 1)
                )
                start_paths.append(set(reader.load_tree(remote.fake.root_id).by_rel()))
            finally:
                reader.close()

    remote.http.event_hooks["request"].append(inspect)
    remote.executor(tracker).execute(run, ops)
    assert len(start_paths) == len(ops)
    assert "docs" not in start_paths[0] and "docs" in start_paths[1]
    assert "original" in start_paths[1] and "original" not in start_paths[2]
    assert "docs/moved" in start_paths[2] and "docs/café" in start_paths[3]
    remote.check_mirror()


def test_trash_logged_for_undo(remote: _Harness) -> None:
    """Remove a whole subtree from the index and log the explicit trash targets."""
    root = remote.fake.root_id
    folder = remote.fake.add_folder("folder")
    child = remote.fake.add_file("child", folder, b"keep in trash")
    file_id = remote.fake.add_file("file", root, b"keep too")
    ops = [_trash("folder", folder), _trash("file", file_id)]
    run, tracker = remote.persist(ops)
    remote.executor(tracker).execute(run, ops)

    remote.reopen()
    assert remote.index.trash_log(run) == [(folder, "folder", True), (file_id, "file", False)]
    assert remote.index.remote_count() == 0
    assert remote.fake.file(child).content == b"keep in trash"
    assert remote.fake.file(child).trashed
    remote.check_mirror()


@pytest.mark.parametrize("break_cycle", [False, True])
def test_move_cycle_deferred_then_failed(remote: _Harness, break_cycle: bool) -> None:
    """Retry a rejected parent cycle after the other moves have had a chance to run."""
    root = remote.fake.root_id
    folder = remote.fake.add_folder("folder")
    child = remote.fake.add_folder("child", folder)
    move = Op(
        OpKind.MOVE,
        "child/folder",
        "renamed",
        file_id=folder,
        name="folder",
        src_parent_id=root,
        parent_id=child,
    )
    other = child if break_cycle else remote.fake.add_folder("other")
    rename = Op(
        OpKind.MOVE,
        "child",
        "renamed",
        file_id=other,
        name="child",
        src_parent_id=folder if break_cycle else root,
        parent_id=root,
    )
    ops = [move, rename]
    run, tracker = remote.persist(ops)
    offset = len(remote.fake.calls)
    report = remote.executor(tracker).execute(run, ops)
    patches = [call for call in remote.fake.calls[offset:] if call.method == "PATCH"]
    assert [call.path.rsplit("/", 1)[-1] for call in patches] == [folder, other, folder]
    assert [call.status for call in patches] == [403, 200, 200 if break_cycle else 403]
    if break_cycle:
        assert report.failed == []
        assert remote.states(run) == [(OpState.DONE, None)] * 2
    else:
        assert report.failed == [(move.rel, "move_rejected")]
        assert remote.states(run) == [(OpState.FAILED, "move_rejected"), (OpState.DONE, None)]
    remote.check_mirror()


def test_file_changed_requeued_then_failed(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requeue a stale file as pending, then fail it after three total attempts."""
    stale = remote.upload("stale")
    other = remote.upload("other")
    (remote.root / stale.rel).write_bytes(b"new longer version")
    ops = [stale, other]
    run, tracker = remote.persist(ops)
    attempts: list[str] = []
    original = remote.index.mark_inflight

    def mark(op: Op) -> None:
        assert (op, OpState.PENDING, None) in remote.index.op_states(run)
        original(op)
        attempts.append(op.rel)

    monkeypatch.setattr(remote.index, "mark_inflight", mark)
    report = remote.executor(tracker).execute(run, ops)
    assert attempts == ["stale", "other", "stale", "stale"]
    assert report.failed == [(stale.rel, "file_in_use")]
    assert remote.states(run) == [(OpState.FAILED, "file_in_use"), (OpState.DONE, None)]
    assert not remote.fake.find(remote.fake.root_id, "stale")
    remote.check_mirror()


def _wrong_md5(response: httpx.Response) -> httpx.Response:
    metadata = response.json()
    metadata["md5Checksum"] = "0" * 32
    return httpx.Response(response.status_code, json=metadata)


@pytest.mark.parametrize(
    ("mismatch_count", "update", "attempts", "writes"),
    [
        (1, False, 1, ["POST", "PATCH"]),
        (1, True, 1, ["PATCH"] * 2),
        (3, True, 2, ["PATCH"] * 4),
        (6, True, 3, ["PATCH"] * 6),
    ],
)
def test_md5_mismatch_retried(
    remote: _Harness,
    mismatch_count: int,
    update: bool,
    attempts: int,
    writes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry consecutive checksum failures immediately and enforce the retry budget."""
    existing = remote.fake.add_file("data", remote.fake.root_id, b"old") if update else None
    op = remote.upload("data", update=existing)
    later = remote.upload("later")
    ops = [op, later]
    run, tracker = remote.persist(ops)
    remaining = mismatch_count
    attempt_order = []
    mark = remote.index.mark_inflight

    def count_attempt(op: Op) -> None:
        attempt_order.append(op.rel)
        mark(op)

    monkeypatch.setattr(remote.index, "mark_inflight", count_attempt)

    def corrupt(req: httpx.Request, response: httpx.Response) -> httpx.Response:
        nonlocal remaining
        if remaining and _upload(req) and response.is_success and response.json()["id"] == _id(op):
            remaining -= 1
            return _wrong_md5(response)
        return response

    remote.responses.append(corrupt)
    offset = len(remote.fake.calls)
    report = remote.executor(tracker, md5_retries=2).execute(run, ops)
    requests = [call for call in remote.fake.calls[offset:] if call.path.startswith("/upload/")]
    assert [(call.method, call.status) for call in requests] == [
        (method, 200) for method in [*writes, "POST"]
    ]
    assert attempt_order == [op.rel] * attempts + [later.rel]
    if mismatch_count == 6:
        assert report.failed == [(op.rel, "md5_mismatch")]
        assert remote.states(run) == [(OpState.FAILED, "md5_mismatch"), (OpState.DONE, None)]
    else:
        assert report.failed == []
        assert remote.states(run) == [(OpState.DONE, None)] * len(ops)
        remote.check_mirror()
    assert remaining == 0
    assert report.uploaded_bytes == later.size + (0 if report.failed else 2 * op.size)
    assert len(remote.fake.find(remote.fake.root_id, op.rel)) == 1


def test_corrupted_create_repairs_single_object_with_verified_md5(remote: _Harness) -> None:
    data = b"good data"
    op = remote.upload("create", data)
    run, tracker = remote.persist([op])

    def corrupt_first_create(req: httpx.Request, response: httpx.Response) -> httpx.Response:
        if req.method == "POST" and _upload(req) and response.is_success:
            remote.fake.set_content(_id(op), b"bad! data")
            return httpx.Response(200, json=remote.fake.file(_id(op)).as_api())
        return response

    remote.responses.append(corrupt_first_create)
    report = remote.executor(tracker).execute(run, [op])

    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert report.failed == [] and report.uploaded_bytes == 2 * len(data)
    (item,) = remote.fake.find(remote.fake.root_id, op.rel)
    assert (item.id, item.content, item.md5) == (
        _id(op),
        data,
        md5(data, usedforsecurity=False).hexdigest(),
    )
    remote.check_mirror()


@pytest.mark.parametrize("err", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "io")])
def test_local_vanished_and_unreadable(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch, err: OSError
) -> None:
    """Classify missing and unreadable sources and continue to the next file."""
    vanished, unreadable, other = [
        remote.upload(rel) for rel in ("vanished", "unreadable", "other")
    ]
    (remote.root / vanished.rel).unlink()
    open_file = os.open

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == remote.root / unreadable.rel:
            raise err
        return open_file(path, *args, **kwargs)

    ops = [vanished, unreadable, other]
    run, tracker = remote.persist(ops)
    monkeypatch.setattr(os, "open", guarded_open)
    report = remote.executor(tracker).execute(run, ops)
    assert report.failed == [("vanished", "local_vanished"), ("unreadable", "local_unreadable")]
    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert remote.states(run) == [
        (OpState.FAILED, "local_vanished"),
        (OpState.FAILED, "local_unreadable"),
        (OpState.DONE, None),
    ]
    remote.check_mirror()


def test_remote_missing_forces_full_listing(remote: _Harness) -> None:
    """Persist a full-refresh request on remote 404 and continue processing."""
    missing = Op(OpKind.TOUCH, "missing", "metadata", file_id=remote.drive.generate_ids(1)[0])
    ops = [missing, remote.upload("other")]
    run, tracker = remote.persist(ops)
    report = remote.executor(tracker).execute(run, ops)
    remote.reopen()
    assert remote.index.get_meta(META_FORCE_FULL) == "1"
    assert report.stop_reason is None and report.failed == [("missing", "remote_missing")]
    assert remote.states(run) == [(OpState.FAILED, "remote_missing"), (OpState.DONE, None)]
    remote.check_mirror()


@pytest.mark.parametrize("remote", [{"quota_limit": 3}], indirect=True)
def test_quota_stops_and_leaves_pending(remote: _Harness) -> None:
    """Stop on the fake's real quota check and leave later content and trash pending."""
    _assert_fatal_upload(remote, "storage_quota_exceeded", "storage_quota_exceeded")


@pytest.mark.parametrize("remote", [{"daily_upload_limit": 3}], indirect=True)
def test_upload_limit_pauses(remote: _Harness) -> None:
    """Stop on the fake's daily upload limit."""
    _assert_fatal_upload(remote, "upload_limit_exceeded", "upload_limit_exceeded")


def _assert_fatal_upload(
    remote: _Harness, reason: str, code: str, *, auth_error: AuthError | None = None
) -> None:
    doomed = remote.upload("doomed")
    later = remote.upload("later", b"x")
    obsolete = remote.fake.add_file("obsolete", remote.fake.root_id, b"x")
    ops = [doomed, later, _trash("obsolete", obsolete)]
    run, tracker = remote.persist(ops)
    remote.tokens.error = auth_error
    report = remote.executor(tracker).execute(run, ops)
    assert report.stop_reason == reason
    assert report.failed == [(doomed.rel, code)]
    assert report.done == Counter() and report.created_ids == [] and report.uploaded_bytes == 0
    assert remote.states(run) == [
        (OpState.FAILED, code),
        (OpState.PENDING, None),
        (OpState.PENDING, None),
    ]
    assert not remote.fake.find(remote.fake.root_id, "later")
    assert not remote.fake.file(obsolete).trashed


@pytest.mark.parametrize(
    ("auth_case", "reason", "code"),
    [
        ("401", "auth_failed", "unauthorized"),
        ("reconnect", "reauth_required", "auth_failed"),
        ("provider", "auth_failed", "auth_failed"),
    ],
)
def test_auth_failure_stops(remote: _Harness, auth_case: str, reason: str, code: str) -> None:
    """Classify both provider failures and exhausted HTTP 401 refreshes."""
    provider_error = None
    if auth_case == "401":
        remote.fake.faults.add(Fault(_upload, "status", status=401, times=2))
    else:
        provider_error = AuthError(needs_reconnect=auth_case == "reconnect")
    _assert_fatal_upload(remote, reason, code, auth_error=provider_error)
    assert remote.tokens.invalidations == (1 if auth_case == "401" else 0)


@pytest.mark.parametrize(
    ("action", "status", "code"),
    [
        ("transport_error", 500, "network_failed"),
        ("status", 503, "server_error"),
        ("status", 429, "rate_limited"),
    ],
)
def test_network_failure_stops(
    remote: _Harness, action: Literal["status", "transport_error"], status: int, code: str
) -> None:
    """Let Drive retry transient errors before stopping the run."""
    remote.fake.faults.add(Fault(_upload, action, status=status, times=3))
    _assert_fatal_upload(remote, "network_failed", code)
    assert sum(call.path.startswith("/upload/") for call in remote.fake.calls) == 3


def test_fatal_finishes_inflight_and_first_reason_wins(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let started uploads finish after a fatal error, even if another also fails."""
    first = remote.upload("first", b"a" * (_CHUNK + 1))
    second = remote.upload("second", b"b" * (_CHUNK + 1))
    survivor = remote.upload("survivor", b"c" * (_CHUNK + 1))
    pending = remote.upload("pending")
    ops = [first, second, survivor, pending]
    run, tracker = remote.persist(ops)
    rendezvous = Barrier(3, timeout=5)
    logged = Event()
    original = remote.index.fail_op

    def fail(op: Op, code: str) -> None:
        original(op, code)
        if op is first:
            logged.set()

    def watch(req: httpx.Request) -> None:
        if _upload(req) and req.url.params.get("uploadType") == "resumable":
            rendezvous.wait()
            if b'"name":"first"' not in req.content:
                assert logged.wait(5)

    monkeypatch.setattr(remote.index, "fail_op", fail)
    remote.http.event_hooks["request"].append(watch)
    remote.fake.faults.add(
        Fault(
            lambda req: _upload(req) and b'"name":"first"' in req.content,
            "status",
            status=403,
            reason="storageQuotaExceeded",
        )
    )
    remote.fake.faults.add(
        Fault(
            lambda req: _upload(req) and b'"name":"second"' in req.content,
            "status",
            status=403,
            reason="uploadLimitExceeded",
        )
    )
    report = remote.executor(tracker, parallel=3).execute(run, ops)
    assert report.stop_reason == "storage_quota_exceeded"
    assert report.failed == [
        (first.rel, "storage_quota_exceeded"),
        (second.rel, "upload_limit_exceeded"),
    ]
    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert remote.states(run) == [
        (OpState.FAILED, "storage_quota_exceeded"),
        (OpState.FAILED, "upload_limit_exceeded"),
        (OpState.DONE, None),
        (OpState.PENDING, None),
    ]
    remote.check_mirror()


def test_cancel_keeps_session_and_inflight_state(remote: _Harness) -> None:
    """Cancel between chunks, reopen the journal, and resume the same upload."""
    content = b"x" * (2 * _CHUNK + 1)
    upload = remote.upload("large", content)
    pending = remote.upload("pending")
    run, tracker = remote.persist([upload, pending])

    def cancel(response: httpx.Response) -> None:
        if _data_put(response.request):
            remote.stop.set()

    remote.http.event_hooks["response"].append(cancel)
    report = remote.executor(tracker).execute(run, [upload, pending])
    assert report.stop_reason == "cancelled" and report.failed == []
    assert not tracker.snapshot().active
    remote.reopen()
    assert remote.states(run) == [(OpState.INFLIGHT, None), (OpState.PENDING, None)]
    assert upload.parent_id is not None
    key = (upload.rel, upload.size, upload.mtime_ns, create_session_kind(upload.parent_id))
    session = remote.index.get_session(*key)
    assert session is not None and session[0] == upload.file_id
    assert not remote.fake.find(remote.fake.root_id, upload.rel)

    remote.stop.clear()
    remote.http.event_hooks["response"].remove(cancel)
    resumed = remote.executor(tracker).execute(run, [upload, pending])
    assert resumed.stop_reason is None and resumed.failed == []
    assert resumed.uploaded_bytes == upload.size - _CHUNK + pending.size
    assert remote.index.get_session(*key) is None
    assert remote.fake.file(_id(upload)).content == content
    remote.check_mirror()


def test_dropped_responses_do_not_duplicate(remote: _Harness) -> None:
    """Lose the first response for each logical write, including resumable chunks."""
    ops = _all_ops(remote)
    large = remote.upload("large", b"x" * (2 * _CHUNK + 1))
    ops.append(large)
    run, tracker = remote.persist(ops)
    dropped: set[tuple[str, str, str]] = set()

    def first_write(req: httpx.Request) -> bool:
        if not FaultPlan.is_write(req) or (req.method == "PUT" and not _data_put(req)):
            return False
        if req.url.params.get("uploadType") == "multipart":
            # The retry has another boundary but the same method and resource.
            key = (req.method, req.url.path, "multipart")
        else:
            key = (req.method, req.url.path, req.headers.get("Content-Range", ""))
        if key in dropped:
            return False
        dropped.add(key)
        return True

    remote.fake.faults.add(Fault(first_write, "drop_response", times=11))
    report = remote.executor(tracker).execute(run, ops)
    assert len(dropped) == 11
    assert report.failed == [] and remote.states(run) == [(OpState.DONE, None)] * len(ops)
    for op in ops:
        if op.kind in {OpKind.MKDIR, OpKind.UPLOAD}:
            assert op.parent_id is not None and op.name is not None
            assert [file.id for file in remote.fake.find(op.parent_id, op.name)] == [_id(op)]
    remote.check_mirror()


def test_failed_create_is_found_even_when_changes_feed_omits_it(remote: _Harness) -> None:
    """Force a listing after all retries fail on an upload that actually committed."""
    root = remote.fake.root_id
    folder = remote.fake.add_folder("docs", properties={OWNER_PROPERTY: _OWNER})
    upload = remote.upload("docs/file", parent=folder)
    indexer = RemoteIndexer(
        remote.drive, remote.index, root, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert indexer.refresh().mode == "full"
    run = remote.index.start_run("test", _WHEN)
    remote.index.save_ops(run, [upload])
    tracker = ProgressTracker(remote.progress_path, run)
    remote.fake.faults.add(Fault(_upload, "drop_response"))
    remote.fake.faults.add(Fault(_upload, "transport_error", times=2))
    report = remote.executor(tracker).execute(run, [upload])
    assert report.stop_reason == "network_failed" and report.created_ids == []
    assert remote.states(run) == [(OpState.FAILED, "network_failed")]
    assert remote.index.get_remote(_id(upload)) is None

    def omit_create(req: httpx.Request, response: httpx.Response) -> httpx.Response:
        if req.url.path != "/drive/v3/changes":
            return response
        body = response.json()
        body["changes"] = [change for change in body["changes"] if change["fileId"] != _id(upload)]
        return httpx.Response(response.status_code, json=body)

    remote.responses.append(omit_create)
    indexer.record_probe(report.created_ids)
    assert indexer.refresh().mode == "full"
    local = {
        "docs": LocalEntry("docs", True, 0, 0),
        upload.rel: LocalEntry(upload.rel, False, upload.size, upload.mtime_ns),
    }
    next_plan = make_plan(
        local,
        remote.index.load_tree(root),
        Matcher(()),
        lambda entry: md5(
            (remote.root / entry.rel).read_bytes(), usedforsecurity=False
        ).hexdigest(),
        PlannerOptions(("docs",), _OWNER),
    )
    assert not next_plan.errors and next_plan.is_empty
    assert [file.id for file in remote.fake.find(folder, "file")] == [_id(upload)]
    remote.check_mirror()


@pytest.mark.parametrize("action", ["crash_before", "crash_after"])
@pytest.mark.parametrize("kind", ["mkdir", "upload", "resumable", "resumable_finish", "trash"])
def test_simulated_crash_propagates(
    remote: _Harness, kind: str, action: Literal["crash_before", "crash_after"]
) -> None:
    """Propagate a process crash from any lane and retain the durable intent."""
    if kind == "mkdir":
        op = remote.mkdir("crash")
        predicate = FaultPlan.is_write
    elif kind == "trash":
        op = _trash("crash", remote.fake.add_file("crash", remote.fake.root_id, b"data"))
        predicate = _metadata(_id(op))
    elif kind == "resumable_finish":
        op = remote.upload("crash", b"x" * (_CHUNK + 1))
        final_range = f"bytes {_CHUNK}-{_CHUNK}/{op.size}"

        def predicate(req: httpx.Request) -> bool:
            return _data_put(req) and req.headers["Content-Range"] == final_range

    else:
        op = remote.upload("crash", b"x" * (_CHUNK + 1) if kind == "resumable" else b"small")
        predicate = _data_put if kind == "resumable" else _upload
    pending = _trash("pending", remote.fake.add_file("pending", remote.fake.root_id, b"data"))
    run, tracker = remote.persist([op, pending])
    remote.fake.faults.add(Fault(predicate, action))
    with pytest.raises(SimulatedCrash):
        remote.executor(tracker).execute(run, [op, pending])
    remote.reopen()
    assert remote.states(run) == [(OpState.INFLIGHT, None), (OpState.PENDING, None)]
    assert [pending_op.db_id for pending_op in remote.index.inflight_ops()] == [op.db_id]
    assert remote.index.trash_log(run) == []
    if kind in {"mkdir", "upload", "resumable_finish"}:
        assert len(remote.fake.find(remote.fake.root_id, op.rel)) == (action == "crash_after")
        assert remote.index.get_remote(_id(op)) is None
    elif kind == "trash":
        assert remote.fake.file(_id(op)).trashed == (action == "crash_after")
        assert remote.index.get_remote(_id(op)) is not None

    report = remote.executor(tracker).execute(run, [op, pending])
    assert report.failed == [] and remote.states(run) == [(OpState.DONE, None)] * 2
    assert len(remote.fake.find(remote.fake.root_id, "crash")) == 1
    if kind == "resumable_finish":
        assert report.uploaded_bytes == (1 if action == "crash_before" else 0)
    if kind == "trash":
        assert (_id(op), "crash", False) in remote.index.trash_log(run)
    remote.check_mirror()


def test_progress_events(remote: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish each phase and each completion, then force the final snapshot to disk."""
    ops = _all_ops(remote)
    run, tracker = remote.persist(ops)
    phases: list[tuple[str, int, int | None]] = []
    transfers: list[tuple[str, int, int]] = []
    completed: list[str] = []
    ended: list[str] = []
    phase, transfer, op_done, transfer_done = (
        tracker.phase,
        tracker.transfer,
        tracker.op_done,
        tracker.transfer_done,
    )

    def record_phase(name: str, *, ops_total: int, bytes_total: int | None = None) -> None:
        phases.append((name, ops_total, bytes_total))
        phase(name, ops_total=ops_total, bytes_total=bytes_total)

    def record_transfer(rel: str, delta: int, total: int) -> None:
        transfers.append((rel, delta, total))
        transfer(rel, delta, total)

    def record_done(rel: str) -> None:
        completed.append(rel)
        op_done(rel)

    def record_end(rel: str) -> None:
        ended.append(rel)
        transfer_done(rel)

    monkeypatch.setattr(tracker, "phase", record_phase)
    monkeypatch.setattr(tracker, "transfer", record_transfer)
    monkeypatch.setattr(tracker, "transfer_done", record_end)
    monkeypatch.setattr(tracker, "op_done", record_done)
    report = remote.executor(tracker).execute(run, ops)
    assert phases == [
        ("mkdir", 1, None),
        ("move", 1, None),
        ("transfer", 4, report.uploaded_bytes),
        ("trash", 1, None),
    ]
    uploads = [op for op in ops if op.kind in {OpKind.UPLOAD, OpKind.UPDATE}]
    assert transfers == [(op.rel, op.size, op.size) for op in uploads]
    assert completed == [op.rel for op in ops]
    assert ended == [op.rel for op in uploads]
    published = read_progress(remote.progress_path)
    assert published is not None
    assert published.phase == "trash" and published.ops_done == 1 and not published.active


def test_progress_write_error_preserves_inflight(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep recovery possible when publishing progress fails after a committed upload."""
    upload = remote.upload("uploaded")
    pending = remote.upload("pending")
    run, tracker = remote.persist([upload, pending])
    error = OSError(errno.ENOSPC, "progress volume full")

    def broken_progress(rel: str, delta: int, size: int) -> None:
        raise error

    monkeypatch.setattr(tracker, "transfer", broken_progress)
    with pytest.raises(RuntimeError, match="publish transfer progress") as caught:
        remote.executor(tracker).execute(run, [upload, pending])
    assert caught.value.__cause__ is error
    remote.reopen()
    assert remote.states(run) == [(OpState.INFLIGHT, None), (OpState.PENDING, None)]
    assert remote.fake.file(_id(upload)).content == b"data"
    assert remote.index.get_remote(_id(upload)) is None


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        (OpKind.TRASH, "db_id"),
        (OpKind.MKDIR, "file_id"),
        (OpKind.UPLOAD, "file_id"),
        (OpKind.MKDIR, "parent_id"),
        (OpKind.UPLOAD, "parent_id"),
        (OpKind.MOVE, "parent_id"),
        (OpKind.MKDIR, "name"),
        (OpKind.UPLOAD, "name"),
    ],
)
def test_validates_entire_plan_before_first_write(
    remote: _Harness, kind: OpKind, field: str
) -> None:
    """Reject a malformed later operation before touching Drive or the journal."""
    good = remote.mkdir("good")
    bad = Op(kind, "bad", "new", file_id="generated-id", parent_id=remote.fake.root_id, name="bad")
    if field != "db_id":
        setattr(bad, field, None)
    run, tracker = remote.persist([good, bad])
    if field == "db_id":
        bad.db_id = None
    offset = len(remote.fake.calls)
    with pytest.raises(ValueError, match=field):
        remote.executor(tracker).execute(run, [good, bad])
    assert len(remote.fake.calls) == offset
    assert remote.states(run) == [(OpState.PENDING, None)] * 2


@pytest.mark.parametrize(
    ("status", "code"), [(400, "bad_request"), (403, "forbidden"), (409, "conflict")]
)
def test_recoverable_drive_errors_allow_later_operations(
    remote: _Harness, status: int, code: str
) -> None:
    """Fail a rejected metadata change without stopping later content."""
    file_id = remote.fake.add_file("touch", remote.fake.root_id, b"same")
    ops = [Op(OpKind.TOUCH, "touch", "metadata", file_id=file_id), remote.upload("later")]
    run, tracker = remote.persist(ops)
    remote.fake.faults.add(
        Fault(_metadata(file_id), "status", status=status, reason="permissionDenied")
    )
    report = remote.executor(tracker).execute(run, ops)
    assert report.stop_reason is None and report.failed == [("touch", code)]
    assert remote.states(run) == [(OpState.FAILED, code), (OpState.DONE, None)]
    remote.check_mirror()


def test_stop_before_first_op_leaves_all_pending(remote: _Harness) -> None:
    """Respect a stop requested before executing the persisted plan."""
    ops = [remote.mkdir("folder"), remote.upload("file")]
    run, tracker = remote.persist(ops)
    remote.stop.set()
    offset = len(remote.fake.calls)
    report = remote.executor(tracker).execute(run, ops)
    assert report.stop_reason == "cancelled"
    assert len(remote.fake.calls) == offset
    assert remote.states(run) == [(OpState.PENDING, None)] * 2


@pytest.mark.parametrize("kind", list(OpKind))
def test_membership_failure_never_mutates_outside_target_and_continues(
    remote: _Harness, kind: OpKind
) -> None:
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    outside_file = remote.fake.add_file("unsafe", outside, b"untouched")
    inside_file = remote.fake.add_file("inside", target, b"inside")
    if kind is OpKind.MKDIR:
        unsafe = remote.mkdir("unsafe", parent=outside)
    elif kind is OpKind.UPLOAD:
        unsafe = remote.upload("unsafe", parent=outside)
    elif kind is OpKind.MOVE:
        unsafe = Op(kind, "unsafe", "renamed", inside_file, parent_id=outside, src_parent_id=target)
    else:
        unsafe = Op(kind, "unsafe", "changed", outside_file)
    valid = remote.upload("safe", parent=target)
    run, tracker = remote.persist([unsafe, valid])
    before = remote.fake.snapshot(outside)
    writes = remote.fake.write_count()

    report = remote.executor(tracker, root_id=target).execute(run, [unsafe, valid])

    assert report.failed == [("unsafe", "remote_changed")] and report.stop_reason is None
    assert remote.states(run) == [(OpState.FAILED, "remote_changed"), (OpState.DONE, None)]
    assert remote.index.get_meta(META_FORCE_FULL) == "1"
    assert remote.fake.write_count() - writes == 1
    assert remote.fake.snapshot(outside) == before
    assert remote.fake.file(inside_file).parents == (target,)


def test_content_refresh_runs_on_scheduler_between_batches(remote: _Harness) -> None:
    ops = [remote.upload(f"file-{i}") for i in range(3)]
    run, tracker = remote.persist(ops)
    now = 0.0
    caller = get_ident()
    refreshes = []
    first_batch = Barrier(2)
    responses = 0
    lock = Lock()

    def refresh() -> None:
        assert get_ident() == caller
        states = [state for state, _ in remote.states(run)]
        assert OpState.INFLIGHT not in states
        refreshes.append(states.count(OpState.DONE))

    def advance_clock(response: httpx.Response) -> None:
        nonlocal now, responses
        if not _upload(response.request):
            return
        with lock:
            responses += 1
            response_number = responses
        if response_number <= 2:
            first_batch.wait(timeout=5)
            now = 60.0

    remote.http.event_hooks["response"].append(advance_clock)
    report = remote.executor(
        tracker, parallel=2, refresh_remote=refresh, clock=lambda: now
    ).execute(run, ops)

    assert report.done == Counter({OpKind.UPLOAD: 3})
    assert refreshes == [0, 0, 0, 2, 3]


def test_stop_before_checksum_retry_keeps_inflight(remote: _Harness) -> None:
    """Leave the uncertain write in flight when cancellation interrupts a retry."""
    upload, later = [remote.upload(rel) for rel in ("file", "later")]
    run, tracker = remote.persist([upload, later])

    def cancel_after_write(req: httpx.Request, response: httpx.Response) -> httpx.Response:
        if _upload(req) and response.is_success:
            remote.stop.set()
            return _wrong_md5(response)
        return response

    remote.responses.append(cancel_after_write)
    report = remote.executor(tracker).execute(run, [upload, later])
    assert report.stop_reason == "cancelled" and report.failed == []
    assert remote.states(run) == [(OpState.INFLIGHT, None), (OpState.PENDING, None)]
    assert remote.fake.file(_id(upload)).content == b"data"
    assert sum(call.path.startswith("/upload/") for call in remote.fake.calls) == 1


def test_created_ids_capped_at_first_twenty(remote: _Harness) -> None:
    """Use the first twenty successful creates for the change-feed probe."""
    ops = [remote.mkdir(f"folder-{i}") for i in range(19)]
    ops.extend(remote.upload(f"file-{i}") for i in range(4))
    run, tracker = remote.persist(ops)
    report = remote.executor(tracker).execute(run, ops)
    assert report.created_ids == [_id(op) for op in ops[:20]]
    assert report.done == Counter({OpKind.MKDIR: 19, OpKind.UPLOAD: 4})
    remote.check_mirror()


def test_fresh_nested_tree_amortizes_drive_reads(tmp_path: Path) -> None:
    """Back up a fresh nested tree within the fake Drive read budget."""
    spec = {f"Docs/a/b/c/file{i:03d}.txt": f"content {i}".encode() for i in range(60)}
    spec.update({f"Docs/x{j}/f{i}.txt": b"y" * (i + 1) for j in range(4) for i in range(10)})
    mirror = make_mirror(tmp_path, spec, parallel=4)
    with mirror.open() as session:
        start = len(mirror.fake.calls)
        summary = session.engine.run(trigger="manual")
        calls = mirror.fake.calls[start:]

    assert summary.result is RunResult.OK
    assert (summary.uploaded, summary.mkdirs) == (100, 8)
    writes = sum(call.method in {"POST", "PATCH", "PUT", "DELETE"} for call in calls)
    reads = len(calls) - writes
    assert writes == 108
    assert reads <= summary.uploaded / 2, f"{reads} reads for {summary.uploaded} uploads"
    mirror.assert_state()


def test_busy_content_phase_refreshes_after_operation_budget(remote: _Harness) -> None:
    """Observe a competing create at the count limit even if the clock stands still."""
    target = remote.fake.add_folder("Target")
    indexer = RemoteIndexer(
        remote.drive, remote.index, target, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    ops = [remote.upload(rel, parent=target) for rel in ("first", "second", "collision")]
    run, tracker = remote.persist(ops)
    indexer.refresh()
    external = []

    def add_competitor(response: httpx.Response) -> None:
        if _upload(response.request) and remote.fake.find(target, "second") and not external:
            external.append(remote.fake.add_file("collision", target, b"external"))

    remote.http.event_hooks["response"].append(add_competitor)
    report = remote.executor(
        tracker,
        root_id=target,
        refresh_remote=indexer.apply_changes,
        refresh_ops=2,
        clock=lambda: 0.0,
    ).execute(run, ops)

    assert report.done == Counter({OpKind.UPLOAD: 2})
    assert report.failed == [("collision", "remote_changed")]
    assert [item.id for item in remote.fake.find(target, "collision")] == external


def test_create_rechecks_cached_ancestry_after_expiry(remote: _Harness) -> None:
    """Recheck a parent that leaves the mirror before its cached proof expires."""
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    folder = remote.fake.add_folder("nested", target)
    ops = [remote.upload(f"nested/{rel}", parent=folder) for rel in ("first", "second")]
    run, tracker = remote.persist(ops)
    now = 0.0

    def move_after_first(response: httpx.Response) -> None:
        nonlocal now
        if _upload(response.request) and now == 0.0:
            remote.drive.update_metadata(folder, add_parent=outside, remove_parents=[target])
            now = 61.0

    remote.http.event_hooks["response"].append(move_after_first)
    report = remote.executor(
        tracker, root_id=target, refresh_remote=lambda: None, refresh_every=900, clock=lambda: now
    ).execute(run, ops)

    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert report.failed == [("nested/second", "remote_changed")]
    assert not remote.fake.find(folder, "second")


def test_create_rechecks_cached_ancestry_after_refresh_reports_drift(remote: _Harness) -> None:
    """Invalidate the cache even if a refresh observes drift on a different path."""
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    folder = remote.fake.add_folder("nested", target)
    ops = [remote.upload(f"nested/{rel}", parent=folder) for rel in ("first", "second")]
    run, tracker = remote.persist(ops)
    moved = False

    def refresh() -> None:
        nonlocal moved
        if moved or not remote.fake.find(folder, "first"):
            return
        remote.fake.add_file("unrelated", target, b"external")
        remote.index.replace_remote(item for _, item in remote.listing().walk())
        remote.drive.update_metadata(folder, add_parent=outside, remove_parents=[target])
        moved = True

    report = remote.executor(
        tracker, root_id=target, refresh_remote=refresh, refresh_ops=1, clock=lambda: 0.0
    ).execute(run, ops)

    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert report.failed == [("nested/second", "remote_changed")]
    assert not remote.fake.find(folder, "second")


def test_remote_changed_refreshes_before_scheduling_the_next_create(remote: _Harness) -> None:
    """An unrelated failed check clears a warm cache and requests an early refresh."""
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    folder = remote.fake.add_folder("nested", target)
    first = remote.upload("nested/first", parent=folder)
    invalid = remote.upload("invalid", parent=outside)
    last = remote.upload("nested/last", parent=folder)
    ops = [first, invalid, last]
    run, tracker = remote.persist(ops)
    moved = False

    def refresh() -> None:
        nonlocal moved
        if not moved and remote.states(run)[1] == (OpState.FAILED, "remote_changed"):
            remote.drive.update_metadata(folder, add_parent=outside, remove_parents=[target])
            moved = True

    report = remote.executor(
        tracker, root_id=target, refresh_remote=refresh, clock=lambda: 0.0
    ).execute(run, ops)

    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert report.failed == [("invalid", "remote_changed"), ("nested/last", "remote_changed")]
    assert not remote.fake.find(folder, "last")


def test_existing_update_rechecks_ancestor_after_cached_create(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm creation cache cannot authorize overwriting an existing item."""
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    folder = remote.fake.add_folder("nested", target)
    item = remote.fake.add_file("report", folder, b"old")
    create = remote.upload("nested/first", parent=folder)
    update = remote.upload("nested/report", b"new", parent=folder, update=item)
    run, tracker = remote.persist([create, update])
    original = remote.drive._multipart

    def multipart(**kwargs: Any) -> RemoteFile:
        if kwargs["update"]:
            remote.drive.update_metadata(folder, add_parent=outside, remove_parents=[target])
        return original(**kwargs)

    monkeypatch.setattr(remote.drive, "_multipart", multipart)
    report = remote.executor(tracker, root_id=target, refresh_remote=lambda: None).execute(
        run, [create, update]
    )

    assert report.done == Counter({OpKind.UPLOAD: 1})
    assert report.failed == [("nested/report", "remote_changed")]
    assert remote.fake.file(item).content == b"old"


@pytest.mark.parametrize("kind", [OpKind.MKDIR, OpKind.UPLOAD, OpKind.MOVE, OpKind.UPDATE])
def test_refresh_revalidates_pending_write(remote: _Harness, kind: OpKind) -> None:
    root = remote.fake.add_folder("Target")
    if kind is OpKind.MKDIR:
        op = remote.mkdir("new", parent=root)
    elif kind is OpKind.UPLOAD:
        op = remote.upload("new", parent=root)
    elif kind is OpKind.MOVE:
        existing = remote.fake.add_file("old", root, b"old")
        op = Op(kind, "new", "renamed", existing, parent_id=root, src_parent_id=root, name="new")
    else:
        existing = remote.fake.add_file("new", root, b"old")
        op = remote.upload("new", parent=root, update=existing)
    run, tracker = remote.persist([op])
    collision = []

    def refresh() -> None:
        if not collision:
            if kind is OpKind.UPDATE:
                remote.fake.set_content(_id(op), b"remote edit")
                collision.append(_id(op))
            else:
                collision.append(remote.fake.add_file("new", root, b"external"))
        remote.index.replace_remote(item for _, item in remote.listing().walk())

    before = remote.fake.write_count()
    report = remote.executor(tracker, root_id=root, refresh_remote=refresh).execute(run, [op])

    assert report.failed == [(op.rel, "remote_changed")]
    assert remote.fake.write_count() == before
    assert [item.id for item in remote.fake.find(root, "new")] == collision


def test_refresh_reports_drift_with_no_pending_write(remote: _Harness) -> None:
    root = remote.fake.add_folder("Target")
    run, tracker = remote.persist([])

    def refresh() -> None:
        if not remote.fake.find(root, "late"):
            remote.fake.add_file("late", root, b"external")
        remote.index.replace_remote(item for _, item in remote.listing().walk())

    report = remote.executor(tracker, root_id=root, refresh_remote=refresh).execute(run, [])

    assert report.failed == [("late", "remote_changed")]
    assert not report.done and remote.fake.write_count() == 0


def test_folder_trash_preserves_nested_unplanned_children(remote: _Harness) -> None:
    root = remote.fake.add_folder("Target")
    folder = remote.fake.add_folder("obsolete", root)
    nested = remote.fake.add_folder("nested", folder)
    old = remote.fake.add_file("old", nested, b"old")
    safe = remote.fake.add_folder("safe", folder)
    remote.fake.add_file("remove", safe, b"old")
    op = _trash("obsolete", folder, affected_files=2)
    run, tracker = remote.persist([op])
    unexpected = []

    def refresh() -> None:
        if not unexpected:
            unexpected.extend(
                [
                    remote.fake.add_native("document", nested),
                    remote.fake.add_file("late", nested, b"external"),
                ]
            )
        remote.index.replace_remote(item for _, item in remote.listing().walk())

    report = remote.executor(tracker, root_id=root, refresh_remote=refresh).execute(run, [op])

    assert all(not remote.fake.file(item).trashed for item in [folder, nested, *unexpected])
    assert remote.fake.file(old).trashed and remote.fake.file(safe).trashed
    assert report.failed == [("obsolete", "remote_changed")]
    assert [(issue.code, issue.rel) for issue in report.warnings] == [
        ("folder-kept-protected", "obsolete")
    ]
    assert sorted(remote.index.trash_log(run)) == sorted(
        [(old, "obsolete/nested/old", False), (safe, "obsolete/safe", True)]
    )


def test_failed_move_keeps_its_source_out_of_folder_trash(remote: _Harness) -> None:
    root = remote.fake.add_folder("Target")
    folder = remote.fake.add_folder("obsolete", root)
    report_id = remote.fake.add_file("report", folder, b"keep")
    old = remote.fake.add_file("old", folder, b"remove")
    move = Op(
        OpKind.MOVE,
        "report",
        "renamed",
        report_id,
        parent_id=root,
        src_parent_id=folder,
        name="report",
    )
    trash = _trash("obsolete", folder)
    run, tracker = remote.persist([move, trash])
    remote.fake.faults.add(Fault(_metadata(report_id), "status", status=403, times=2))

    report = remote.executor(tracker, root_id=root).execute(run, [move, trash])

    assert not remote.fake.file(folder).trashed and not remote.fake.file(report_id).trashed
    assert remote.fake.file(old).trashed
    assert report.failed == [("report", "move_rejected"), ("obsolete", "remote_changed")]
    assert remote.index.trash_log(run) == [(old, "obsolete/old", False)]


def test_multipart_update_checks_scope_after_executor_dispatch(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = remote.fake.add_folder("Target")
    outside = remote.fake.add_folder("Outside")
    existing = remote.fake.add_file("report", target, b"old")
    op = remote.upload("report", b"new", parent=target, update=existing)
    run, tracker = remote.persist([op])
    original = remote.drive._multipart

    def multipart(**kwargs: Any) -> RemoteFile:
        remote.drive.update_metadata(existing, add_parent=outside, remove_parents=[target])
        return original(**kwargs)

    monkeypatch.setattr(remote.drive, "_multipart", multipart)
    report = remote.executor(tracker, root_id=target).execute(run, [op])

    assert report.failed == [("report", "remote_changed")]
    assert remote.fake.file(existing).parents == (outside,)
    assert remote.fake.file(existing).content == b"old"


@pytest.mark.parametrize("probe_fails", [False, True])
def test_lost_trash_response_is_undoable(tmp_path: Path, probe_fails: bool) -> None:
    mirror = make_mirror(tmp_path, {"Docs/anchor": b"keep", "Docs/report": b"remove"})
    with mirror.open() as session:
        assert session.engine.run(trigger="manual").result is RunResult.OK
        (docs,) = mirror.fake.find(mirror.target, "Docs")
        (item,) = mirror.fake.find(docs.id, "report")
        (mirror.root / "Docs/report").unlink()
        mirror.fake.faults.add(Fault(_metadata(item.id), "drop_response"))
        mirror.fake.faults.add(Fault(_metadata(item.id), "transport_error", times=7))
        if probe_fails:
            mirror.fake.faults.add(
                Fault(
                    lambda req: (
                        req.method == "GET"
                        and req.url.path == f"/drive/v3/files/{item.id}"
                        and mirror.fake.file(item.id).trashed
                    ),
                    "transport_error",
                    times=16,
                )
            )

        summary = session.engine.run(trigger="manual")
        assert mirror.fake.file(item.id).trashed
        if probe_fails:
            assert summary.result is RunResult.FAILED
            assert [op.file_id for op in session.index.inflight_ops()] == [item.id]
            recovered = session.engine.run(trigger="manual")
            assert recovered.result is RunResult.OK
        else:
            assert summary.result is RunResult.OK
            assert summary.trashed == 1
        undo = session.engine.undo()
        assert undo.untrashed == 1
        assert (mirror.root / "Docs/report").read_bytes() == b"remove"


def test_upload_rejects_source_outside_replaced_ancestor(remote: _Harness, tmp_path: Path) -> None:
    op = remote.upload("sub/report", b"public")
    run, tracker = remote.persist([op])
    outside = tmp_path / "outside"
    (remote.root / "sub").rename(outside)
    (outside / "report").write_bytes(b"secret")
    os.utime(outside / "report", ns=(op.mtime_ns, op.mtime_ns))
    (remote.root / "sub").symlink_to(outside, target_is_directory=True)
    before = remote.fake.write_count()

    report = remote.executor(tracker).execute(run, [op])

    assert report.failed == [(op.rel, "file_in_use")]
    assert remote.fake.write_count() == before


def test_cancelled_multipart_retry_starts_no_write(
    remote: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = remote.upload("report")
    run, tracker = remote.persist([op])
    remote.fake.faults.add(Fault(_upload, "status", status=503))
    monkeypatch.setattr(remote.drive, "_sleep", lambda _: remote.stop.set())
    report = remote.executor(tracker).execute(run, [op])

    assert report.stop_reason == "cancelled" and not report.done
    assert [call.status for call in remote.fake.calls if call.path.startswith("/upload/")] == [503]
    assert not remote.fake.find(remote.fake.root_id, "report")
