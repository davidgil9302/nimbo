"""Exercise engine entry points against a real journal and the fake Drive API."""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import md5
from pathlib import Path
from threading import Event

import httpx
import pytest

from nimbo.core.config import Config, OAuthConfig, SafetyConfig, SourceConfig, TargetConfig
from nimbo.core.drive import DriveClient
from nimbo.core.engine import Engine, EngineDeps
from nimbo.core.errors import AuthError, FileChanged, LockedError
from nimbo.core.executor import ExecReport
from nimbo.core.index import (
    META_ACCOUNT,
    META_CHANGES_RELIABLE,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    META_LAST_DEEP,
    META_QUOTA_LIMIT,
    META_ROOT_UUID,
    META_TARGET_ID,
    Index,
)
from nimbo.core.lock import RunLock
from nimbo.core.model import (
    OWNER_PROPERTY,
    SENTINEL_NAME,
    Op,
    OpKind,
    OpState,
    PlanIssue,
    RunResult,
    RunSummary,
    drive_ms,
)
from nimbo.core.paths import Paths
from nimbo.core.progress import ProgressTracker, read_progress
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.safety import write_approval, write_sentinel
from nimbo.core.transfer import Downloader, Uploader, create_session_kind
from tests.fakedrive import FakeDrive, Fault, FaultPlan, SimulatedCrash

_OWNER = "source-owner"
_DAY = datetime(2026, 11, 23, 18, tzinfo=UTC)
_CHUNK = 256 * 1024
_MTIME = 1_772_000_000_123_456_789


@dataclass
class _Clock:
    now: datetime = _DAY

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _Notifier:
    calls: list[tuple[RunSummary, bool]] = field(default_factory=list)

    def run_finished(self, summary: RunSummary, *, manual: bool) -> None:
        self.calls.append((summary, manual))


class _Tokens:
    def __init__(self, fake: FakeDrive) -> None:
        self.value = fake.oauth.add_access_token("test-token")
        self.error: AuthError | None = None

    def token(self) -> str:
        if self.error is not None:
            raise self.error
        return self.value

    def invalidate(self) -> None:
        pass


@dataclass
class _Harness:
    deps: EngineDeps
    fake: FakeDrive
    tokens: _Tokens
    clock: _Clock

    @property
    def engine(self) -> Engine:
        return Engine(self.deps)

    @property
    def root(self) -> Path:
        return self.deps.cfg.source.root

    @property
    def paths(self) -> Paths:
        return self.deps.paths

    @property
    def index(self) -> Index:
        return self.deps.index

    @property
    def target(self) -> str:
        return self.deps.cfg.target.folder_id

    def put(self, rel: str, data: bytes = b"example") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(_MTIME, _MTIME))
        return path

    def run(self, *, result: RunResult = RunResult.OK, **kwargs: object) -> RunSummary:
        summary = self.engine.run(trigger="manual", **kwargs)
        assert summary.result is result, summary
        if summary.run_id is not None:
            assert self.index.get_run(summary.run_id) == summary
        return summary

    def reopen(self) -> None:
        self.index.close()
        self.deps.index = Index(self.paths.db_file)
        self.deps.uploader = Uploader(
            self.deps.drive, self.index, chunk_size=_CHUNK, multipart_max=_CHUNK
        )

    def assert_mirror(self) -> None:
        expected = {
            path.relative_to(self.root).as_posix(): (
                path.is_dir(),
                0 if path.is_dir() else path.stat().st_size,
                None
                if path.is_dir()
                else md5(path.read_bytes(), usedforsecurity=False).hexdigest(),
            )
            for path in (self.root / "Docs").rglob("*")
        }
        expected["Docs"] = (True, 0, None)
        assert self.fake.snapshot(self.target) == expected
        tree = self.index.load_tree(self.target)
        assert set(tree.by_rel()) == set(expected)
        assert all(len(items) == 1 for items in tree.by_rel().values())
        folders = [self.target]
        for folder in folders:
            children = list(self.deps.drive.list_children([folder]))
            assert len({child.name for child in children}) == len(children)
            folders.extend(child.id for child in children if child.is_folder)


@pytest.fixture
def mirror(
    tmp_path: Path,
    source_root: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Harness]:
    clock = _Clock()
    monkeypatch.setattr("nimbo.core.index.utc_now_iso", lambda: clock().isoformat())
    fake = FakeDrive(clock=lambda: clock.now.timestamp(), **getattr(request, "param", {}))
    tokens = _Tokens(fake)
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.ensure()
    write_sentinel(source_root, _OWNER)
    target = fake.add_folder("Backup")
    cfg = Config(
        SourceConfig(source_root, ("Docs",), require_mount=False),
        TargetConfig(target),
        OAuthConfig("test-client", "test-secret"),
    )
    with fake.client() as client:
        drive = DriveClient(
            client,
            tokens,
            RateLimiter(
                reads_per_second=10_000,
                writes_per_second=10_000,
                clock=lambda: 0.0,
                sleep=lambda _: pytest.fail("unexpected limiter wait"),
            ),
            RetryPolicy(max_attempts=3, base_delay=0),
            sleep=lambda _: None,
        )
        index = Index(paths.db_file)
        deps = EngineDeps(
            cfg,
            paths,
            index,
            drive,
            Uploader(drive, index, chunk_size=_CHUNK, multipart_max=_CHUNK),
            Downloader(drive),
            _Notifier(),
            clock=clock,
            network_check=lambda: True,
        )
        harness = _Harness(deps, fake, tokens, clock)
        harness.put("Docs/keep.txt", b"keep")
        try:
            yield harness
        finally:
            harness.index.close()


def _patch(request: httpx.Request) -> bool:
    return request.method == "PATCH"


def test_first_run_uploads_everything(mirror: _Harness) -> None:
    mirror.put("Docs/sub/a.txt")
    mirror.put("Docs/zero", b"")
    summary = mirror.run()
    assert (summary.mkdirs, summary.uploaded) == (2, 3)
    mirror.assert_mirror()
    assert mirror.index.get_meta(META_ROOT_UUID) == _OWNER
    assert mirror.index.get_meta(META_TARGET_ID) == mirror.target
    assert mirror.index.get_meta(META_LAST_DEEP) == _DAY.isoformat()
    assert mirror.index.get_meta(META_ACCOUNT)
    assert mirror.index.get_meta(META_QUOTA_LIMIT)
    assert mirror.index.get_meta(META_CHANGES_TOKEN)


def test_second_run_makes_no_writes(mirror: _Harness) -> None:
    mirror.put("Docs/sub/a")
    mirror.run()
    writes = mirror.fake.write_count()

    assert mirror.run().uploaded == 0

    assert mirror.fake.write_count() == writes
    mirror.assert_mirror()


def test_edit_rename_delete_cycle(mirror: _Harness) -> None:
    mirror.put("Docs/edit", b"old")
    rename = mirror.put("Docs/rename")
    delete = mirror.put("Docs/delete", b"delete")
    mirror.run()
    folder = mirror.fake.find(mirror.target, "Docs")[0].id
    renamed_id = mirror.fake.find(folder, "rename")[0].id
    mirror.put("Docs/edit", b"new!")
    rename.rename(rename.with_name("renamed"))
    delete.unlink()

    summary = mirror.run()

    assert (summary.updated, summary.moved, summary.trashed) == (1, 1, 1)
    assert mirror.fake.find(folder, "renamed")[0].id == renamed_id
    mirror.assert_mirror()


def test_brake_needs_confirmation_then_approval(mirror: _Harness) -> None:
    mirror.deps.cfg = replace(mirror.deps.cfg, safety=SafetyConfig(max_deletes=0))
    paths = [mirror.put(f"Docs/{i:03}") for i in range(502)]
    folder = mirror.fake.add_folder("Docs", mirror.target, properties={OWNER_PROPERTY: _OWNER})
    for path in paths:
        mirror.fake.add_file(path.name, folder, b"example", properties={OWNER_PROPERTY: _OWNER})
        path.unlink()
    writes = mirror.fake.write_count()

    blocked = mirror.run(result=RunResult.NEEDS_CONFIRMATION)
    assert blocked.fingerprint
    assert blocked.trash_preview == [f"Docs/{i:03}\t1" for i in range(500)]
    assert mirror.index.op_states(blocked.run_id) == []
    assert mirror.fake.write_count() == writes
    write_approval(mirror.paths.approval_file, blocked.fingerprint, now=mirror.clock())

    assert mirror.run().trashed == 502
    assert not mirror.paths.approval_file.exists()


def test_allow_brakes_flag(mirror: _Harness) -> None:
    mirror.deps.cfg = replace(mirror.deps.cfg, safety=SafetyConfig(max_deletes=0))
    path = mirror.put("Docs/delete")
    mirror.run()
    path.unlink()

    assert mirror.run(allow_brakes=True).trashed == 1


def test_sentinel_missing_skips(mirror: _Harness) -> None:
    (mirror.root / SENTINEL_NAME).unlink()
    assert mirror.run(result=RunResult.SKIPPED).reason == "sentinel_missing"
    assert mirror.fake.calls == []


def test_sentinel_mismatch_skips(mirror: _Harness) -> None:
    mirror.index.set_meta(META_ROOT_UUID, "different-owner")
    assert mirror.run(result=RunResult.SKIPPED).reason == "sentinel_mismatch"
    assert mirror.fake.calls == []


def test_folder_missing_skips(mirror: _Harness) -> None:
    (mirror.root / "Docs").rename(mirror.root / "Moved")
    assert mirror.run(result=RunResult.SKIPPED).reason == "folder_missing"
    assert mirror.fake.calls == []


def test_offline_skips(mirror: _Harness) -> None:
    mirror.deps.network_check = lambda: False
    assert mirror.run(result=RunResult.SKIPPED).reason == "network_unavailable"
    assert mirror.fake.calls == []


def test_reauth_required_fails(mirror: _Harness) -> None:
    mirror.tokens.error = AuthError(needs_reconnect=True, code="reauth_required")
    assert mirror.run(result=RunResult.FAILED).reason == "reauth_required"
    assert mirror.fake.calls == []


@pytest.mark.parametrize("mirror", [{"quota_limit": 1}], indirect=True)
def test_storage_insufficient_fails(mirror: _Harness) -> None:
    summary = mirror.run(result=RunResult.FAILED)
    assert summary.reason == "storage_insufficient"
    assert mirror.fake.write_count() == 0
    assert mirror.index.op_states(summary.run_id) == []


def test_target_missing_fails(mirror: _Harness) -> None:
    mirror.deps.cfg = replace(mirror.deps.cfg, target=TargetConfig("missing-folder"))
    assert mirror.run(result=RunResult.FAILED).reason == "target_missing"
    assert mirror.fake.write_count() == 0


def test_foreign_owner_fails(mirror: _Harness) -> None:
    mirror.fake.add_folder("Docs", mirror.target, properties={OWNER_PROPERTY: "foreign"})
    summary = mirror.run(result=RunResult.FAILED)
    assert summary.reason == "foreign-owner"
    assert mirror.fake.write_count() == 0


def test_locked_skips(mirror: _Harness) -> None:
    with RunLock(mirror.paths.lock_file):
        summary = mirror.run(result=RunResult.SKIPPED)
        assert mirror.index.list_runs() == []
        assert (summary.run_id, summary.reason) == (None, "already_running")
        with pytest.raises(LockedError):
            mirror.engine.preview()
        with pytest.raises(LockedError):
            mirror.engine.verify()
        with pytest.raises(LockedError):
            mirror.engine.undo()
    assert mirror.run().run_id == 1


def test_cancel_then_rerun_converges(mirror: _Harness) -> None:
    stop = Event()
    original = mirror.deps.drive.create_folder

    def create_and_stop(**kwargs: object):
        item = original(**kwargs)
        stop.set()
        return item

    mirror.deps.drive.create_folder = create_and_stop
    summary = mirror.run(result=RunResult.CANCELLED, stop=stop)
    assert summary.reason == "cancelled"
    assert summary.mkdirs == 1
    assert mirror.index.folder_stats()["Docs"].files_pending == 1

    mirror.deps.drive.create_folder = original
    mirror.run()
    mirror.assert_mirror()


def test_reconcile_inflight_after_crash(mirror: _Harness) -> None:
    index = mirror.index
    remote_id = mirror.fake.add_folder("Docs", mirror.target, properties={OWNER_PROPERTY: _OWNER})
    run_id = index.start_run("manual", _DAY.isoformat())
    op = Op(OpKind.MKDIR, "Docs", "new", file_id=remote_id)
    missing = Op(OpKind.UPLOAD, "Docs/keep.txt", "new", file_id="never-created")
    no_id = Op(OpKind.UPLOAD, "Docs/other", "new")
    index.save_ops(run_id, [op, missing, no_id])
    for pending in (op, missing, no_id):
        index.mark_inflight(pending)

    summary = mirror.run()

    assert (summary.mkdirs, summary.uploaded) == (0, 1)
    assert [state for _, state, _ in index.op_states(run_id)] == [OpState.ABANDONED] * 3
    mirror.assert_mirror()


def test_upload_limit_pauses_then_resumes(mirror: _Harness) -> None:
    path = mirror.put("Docs/large", b"x" * (_CHUNK + 1))
    mirror.fake.faults.add(
        Fault(
            lambda req: req.method == "PUT" and "upload_id" in req.url.params,
            "status",
            status=403,
            reason="uploadLimitExceeded",
            skip=1,
        )
    )
    first = mirror.run(result=RunResult.PAUSED)
    assert first.reason == "upload_limit_exceeded"
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    key = ("Docs/large", path.stat().st_size, _MTIME, create_session_kind(docs))
    session = mirror.index.get_session(*key)
    assert session is not None

    mirror.clock.now += timedelta(days=1)
    second = mirror.run()

    assert second.uploaded == 1
    assert mirror.index.get_session(*key) is None
    assert mirror.fake.file(session[0]).size == path.stat().st_size
    mirror.assert_mirror()


@pytest.mark.parametrize("mirror", [{"daily_upload_limit": 4}], indirect=True)
def test_daily_upload_limit_leaves_pending_files_until_next_day(mirror: _Harness) -> None:
    mirror.put("Docs/next", b"next")
    summary = mirror.run(result=RunResult.PAUSED)
    assert (summary.reason, summary.uploaded) == ("upload_limit_exceeded", 1)
    stat = mirror.index.folder_stats()["Docs"]
    assert (stat.files_pending, stat.bytes_pending, stat.last_ok) == (1, 4, None)

    mirror.clock.now += timedelta(days=1)
    assert mirror.run().uploaded == 1
    mirror.assert_mirror()


def test_partial_when_file_in_use(mirror: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    path = mirror.put("Docs/busy", b"busy")
    original = mirror.deps.uploader.upload

    def busy(req, **kwargs):
        if req.rel == "Docs/busy":
            raise FileChanged()
        return original(req, **kwargs)

    monkeypatch.setattr(mirror.deps.uploader, "upload", busy)
    summary = mirror.run(result=RunResult.PARTIAL)
    assert summary.failed == [("Docs/busy", "file_in_use")]
    assert summary.uploaded == 1
    stat = mirror.index.folder_stats()["Docs"]
    assert (stat.files_local, stat.bytes_local, stat.files_pending, stat.bytes_pending) == (
        2,
        8,
        1,
        4,
    )
    assert stat.last_ok is None and path.exists()


def test_undo_restores_local_files_and_untrashes(mirror: _Harness) -> None:
    single = mirror.put("Docs/single", b"single")
    nested = mirror.put("Docs/sub/nested", b"nested")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    sub = mirror.fake.find(docs, "sub")[0].id
    single.unlink()
    nested.unlink()
    nested.parent.rmdir()
    trashed = mirror.run()
    folder_mtime = mirror.fake.file(sub).modified_ms

    restored = mirror.engine.undo()

    assert (restored.run_id, restored.untrashed, restored.restored_files) == (trashed.run_id, 2, 2)
    assert restored.failed == []
    assert (single.read_bytes(), nested.read_bytes()) == (b"single", b"nested")
    assert nested.stat().st_mtime_ns == drive_ms(_MTIME) * 1_000_000
    assert nested.parent.stat().st_mtime_ns == folder_mtime * 1_000_000
    mirror.assert_mirror()


def test_undo_never_overwrites(mirror: _Harness) -> None:
    path = mirror.put("Docs/replaced", b"old")
    mirror.run()
    path.unlink()
    trashed = mirror.run()
    path.write_bytes(b"new")

    restored = mirror.engine.undo(trashed.run_id)

    assert restored.untrashed == 1
    assert restored.restored_files == 0
    assert restored.skipped_existing == ["Docs/replaced"]
    assert path.read_bytes() == b"new"


def test_verify_detects_silent_remote_corruption_and_repairs(mirror: _Harness) -> None:
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    item = mirror.fake.find(docs, "keep.txt")[0]
    key = ("Docs/keep.txt", 4, _MTIME)
    mirror.index.put_hash(*key, md5(b"oops", usedforsecurity=False).hexdigest())
    mirror.fake.set_content(item.id, b"oops", keep_modified=True)

    audit = mirror.engine.verify(repair=True)

    assert audit.mismatched == ["Docs/keep.txt"]
    assert audit.repaired is not None
    assert (audit.repaired.result, audit.repaired.trigger, audit.repaired.updated) == (
        RunResult.OK,
        "verify",
        1,
    )
    mirror.assert_mirror()


def test_verify_reports_missing_extra_mismatched_and_duplicate_paths(mirror: _Harness) -> None:
    mirror.put("Docs/edited")
    mirror.run()
    mirror.put("Docs/missing")
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    edited = mirror.fake.find(docs, "edited")[0]
    mirror.fake.set_content(edited.id, b"different size")
    mirror.fake.add_file("extra", docs, b"remote")
    mirror.fake.add_file("keep.txt", docs, b"duplicate")
    writes = mirror.fake.write_count()

    report = mirror.engine.verify()

    assert report.missing_remote == ["Docs/missing"]
    assert report.extra_remote == ["Docs/extra"]
    assert report.mismatched == ["Docs/edited"]
    assert report.duplicates == ["Docs/keep.txt"]
    assert report.repaired is None
    assert mirror.fake.write_count() == writes


def test_pruning_sessions_and_hashes_after_execution(mirror: _Harness) -> None:
    missing_rel = "Docs/absent"
    mirror.index.put_hash(missing_rel, 3, _MTIME, "digest")
    mirror.index.put_session(missing_rel, 3, _MTIME, "create", "old-id", "https://example.test")
    mirror.clock.now += timedelta(days=10)
    mirror.fake.faults.add(
        Fault(
            lambda req: req.method == "POST" and req.url.params.get("uploadType") == "multipart",
            "status",
            status=403,
            reason="uploadLimitExceeded",
        )
    )

    mirror.run(result=RunResult.PAUSED)
    assert mirror.index.get_hash(missing_rel, 3, _MTIME) == "digest"
    assert mirror.index.get_session(missing_rel, 3, _MTIME, "create") is None

    mirror.run()
    assert mirror.index.get_hash(missing_rel, 3, _MTIME) is None


def test_target_change_resets_index(mirror: _Harness) -> None:
    mirror.run()
    before = mirror.fake.snapshot(mirror.target)
    old_target = mirror.target
    new_target = mirror.fake.add_folder("New backup")
    mirror.deps.cfg = replace(mirror.deps.cfg, target=TargetConfig(new_target))

    summary = mirror.run()

    assert (summary.mkdirs, summary.uploaded) == (1, 1)
    assert mirror.index.get_meta(META_TARGET_ID) == new_target
    assert mirror.index.get_remote(old_target) is None
    assert mirror.fake.snapshot(old_target) == before
    mirror.assert_mirror()


def test_target_change_discards_sessions_for_the_previous_parent(mirror: _Harness) -> None:
    path = mirror.put("Docs/large", b"x" * (_CHUNK + 1))
    mirror.fake.faults.add(
        Fault(lambda req: req.method == "PUT" and "upload_id" in req.url.params, "crash_after")
    )
    with pytest.raises(SimulatedCrash):
        mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    key = ("Docs/large", path.stat().st_size, _MTIME, create_session_kind(docs))
    session = mirror.index.get_session(*key)
    assert session is not None
    old_target = mirror.target
    previous = mirror.fake.snapshot(old_target)
    mirror.deps.cfg = replace(
        mirror.deps.cfg, target=TargetConfig(mirror.fake.add_folder("New backup"))
    )

    mirror.run()

    assert mirror.fake.snapshot(old_target) == previous
    assert mirror.index.get_session(*key) is None
    docs = mirror.fake.find(mirror.target, "Docs")[0]
    assert mirror.fake.find(docs.id, "large")[0].id != session[0]
    mirror.assert_mirror()


@pytest.mark.parametrize("move_ancestor", [False, True], ids=["file", "ancestor"])
def test_trash_fails_closed_if_item_leaves_target_before_live_check(
    mirror: _Harness, monkeypatch: pytest.MonkeyPatch, move_ancestor: bool
) -> None:
    path = mirror.put("Docs/deleted")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    file_id = mirror.fake.find(docs, path.name)[0].id
    outside = mirror.fake.add_folder("Outside")
    path.unlink()
    get = mirror.deps.drive.get
    moved = False

    def move_then_get(item_id: str):
        nonlocal moved
        if item_id == file_id and not moved:
            moved = True
            moved_id, old_parent = (docs, mirror.target) if move_ancestor else (file_id, docs)
            mirror.deps.drive.update_metadata(
                moved_id, add_parent=outside, remove_parents=[old_parent]
            )
        return get(item_id)

    monkeypatch.setattr(mirror.deps.drive, "get", move_then_get)
    partial = mirror.run(result=RunResult.PARTIAL)
    assert partial.failed == [("Docs/deleted", "remote_changed")]
    assert mirror.index.get_meta(META_FORCE_FULL) == "1"
    item = mirror.fake.file(file_id)
    assert item.parents == ((docs,) if move_ancestor else (outside,)) and not item.trashed
    outside_tree = mirror.fake.snapshot(outside)
    assert mirror.index.trash_log(partial.run_id) == []

    mirror.run()
    mirror.assert_mirror()
    writes = mirror.fake.write_count()
    mirror.run()
    assert mirror.fake.write_count() == writes
    assert mirror.fake.file(file_id) == item
    assert mirror.fake.snapshot(outside) == outside_tree


@pytest.mark.parametrize("move_after_planning", [True, False])
def test_resumable_create_never_writes_to_parent_moved_outside_target(
    mirror: _Harness, move_after_planning: bool
) -> None:
    path = mirror.put("Docs/large", b"x" * (_CHUNK + 1))
    mirror.fake.faults.add(
        Fault(lambda req: req.method == "PUT" and "upload_id" in req.url.params, "crash_after")
    )
    with pytest.raises(SimulatedCrash):
        mirror.run()
    old_parent = mirror.fake.find(mirror.target, "Docs")[0].id
    key = ("Docs/large", path.stat().st_size, _MTIME, create_session_kind(old_parent))
    session = mirror.index.get_session(*key)
    assert session is not None
    outside = mirror.fake.add_folder("Outside")
    children = mirror.fake.snapshot(old_parent)
    mirror.reopen()

    def move_folder() -> None:
        mirror.deps.drive.update_metadata(
            old_parent, add_parent=outside, remove_parents=[mirror.target]
        )

    @contextmanager
    def move_at_execution() -> Iterator[None]:
        move_folder()
        yield

    if move_after_planning:
        guard = mirror.deps.idle_inhibitor
        mirror.deps.idle_inhibitor = move_at_execution
        partial = mirror.run(result=RunResult.PARTIAL)
        assert ("Docs/large", "remote_changed") in partial.failed
        assert mirror.index.get_meta(META_FORCE_FULL) == "1"
        assert not mirror.fake.find(mirror.target, "Docs")
        mirror.deps.idle_inhibitor = guard
    else:
        move_folder()

    mirror.run()
    mirror.assert_mirror()
    assert mirror.index.get_session(*key) == session
    new_parent = mirror.fake.find(mirror.target, "Docs")[0].id
    assert new_parent != old_parent
    assert mirror.fake.find(new_parent, path.name)[0].id != session[0]
    assert mirror.fake.snapshot(old_parent) == children
    assert not mirror.fake.find(old_parent, path.name)
    writes = mirror.fake.write_count()
    mirror.run()
    assert mirror.fake.write_count() == writes


def test_phase_refresh_does_not_reprobe_consumed_creates(mirror: _Harness) -> None:
    mirror.run()
    assert mirror.index.get_meta(META_CHANGES_RELIABLE) == "1"

    mirror.fake.calls.clear()
    mirror.run()

    assert mirror.index.get_meta(META_CHANGES_RELIABLE) == "1"
    assert not any(call.path.endswith("/startPageToken") for call in mirror.fake.calls)


def test_undo_refuses_historical_log_from_another_target(mirror: _Harness) -> None:
    path = mirror.put("Docs/deleted")
    mirror.run()
    path.unlink()
    trashed = mirror.run()
    file_id = mirror.index.trash_log(trashed.run_id)[0][0]
    mirror.deps.cfg = replace(
        mirror.deps.cfg, target=TargetConfig(mirror.fake.add_folder("New backup"))
    )
    writes = mirror.fake.write_count()

    result = mirror.engine.undo(trashed.run_id)

    assert result.failed == [("Docs/deleted", "unsafe_restore_target")]
    assert mirror.fake.write_count() == writes
    assert mirror.fake.file(file_id).trashed
    assert not path.exists()


def test_folder_stats_updated(mirror: _Harness) -> None:
    mirror.deps.cfg = replace(
        mirror.deps.cfg, source=replace(mirror.deps.cfg.source, folders=("Docs", "Other"))
    )
    mirror.put("Other/backup", b"data")

    preview = mirror.engine.preview()
    assert preview.plan.stats.total_ops == 4
    assert mirror.fake.write_count() == 0
    assert mirror.index.list_runs() == []
    for stat in mirror.index.folder_stats().values():
        assert (stat.files_local, stat.bytes_local, stat.files_pending, stat.bytes_pending) == (
            1,
            4,
            1,
            4,
        )
        assert stat.last_ok is None

    summary = mirror.run()

    for stat in mirror.index.folder_stats().values():
        assert (stat.files_pending, stat.bytes_pending) == (0, 0)
        assert stat.last_ok == stat.updated_at == summary.finished_at


def test_progress_file_removed_and_notifier_called(
    mirror: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = mirror.deps.network_check
    phase = ProgressTracker.phase

    def record_phase(tracker: ProgressTracker, name: str, **kwargs: object) -> None:
        seen.append(name)
        phase(tracker, name, **kwargs)

    monkeypatch.setattr(ProgressTracker, "phase", record_phase)

    def network() -> bool:
        status = read_progress(mirror.paths.progress_file)
        assert status is not None
        assert status.phase == "preflight"
        return original()

    @contextmanager
    def inhibitor() -> Iterator[None]:
        status = read_progress(mirror.paths.progress_file)
        assert status is not None
        seen.append("inhibit")
        yield

    mirror.deps.network_check = network
    mirror.deps.idle_inhibitor = inhibitor
    summary = mirror.engine.run(trigger="timer")
    assert summary.result is RunResult.OK
    assert seen == [
        "preflight",
        "listing",
        "scanning",
        "planning",
        "inhibit",
        "mkdir",
        "move",
        "transfer",
        "trash",
    ]
    assert not mirror.paths.progress_file.exists()
    assert mirror.deps.notifier.calls == [(summary, False)]

    mirror.deps.network_check = lambda: False
    blocked = mirror.run(result=RunResult.SKIPPED)
    assert not mirror.paths.progress_file.exists()
    assert seen.count("inhibit") == 1
    assert mirror.deps.notifier.calls[-1] == (blocked, True)


def test_paused_folder_untouched(mirror: _Harness) -> None:
    mirror.deps.cfg = replace(
        mirror.deps.cfg,
        source=replace(mirror.deps.cfg.source, folders=("Docs", "Paused"), paused=("Paused",)),
    )
    folder = mirror.fake.add_folder("Paused", mirror.target)
    mirror.fake.add_file("only-remote", folder, b"retain")
    mirror.put("Paused/only-local")

    mirror.run()

    assert set(mirror.fake.snapshot(folder)) == {"only-remote"}
    assert set(mirror.index.folder_stats()) == {"Docs"}


@pytest.mark.parametrize("write", [1, 2, 3, 4, 5, 6, 7])
def test_crash_mid_run_then_reconcile_converges(mirror: _Harness, write: int) -> None:
    mirror.put("Docs/sub/nested", b"nested")
    mirror.put("Docs/large", b"x" * (_CHUNK + 1))
    mirror.fake.faults.add(Fault(FaultPlan.is_write, "crash_after", skip=write - 1))

    with pytest.raises(SimulatedCrash):
        mirror.run()
    assert not mirror.paths.progress_file.exists()
    assert mirror.index.inflight_ops()
    assert mirror.deps.notifier.calls == []

    mirror.reopen()
    for _ in range(3):
        summary = mirror.engine.run(trigger="manual")
        if summary.result is RunResult.OK:
            break
    assert summary.result is RunResult.OK, summary
    assert mirror.index.inflight_ops() == []
    mirror.assert_mirror()

    writes = mirror.fake.write_count()
    mirror.run()
    assert mirror.fake.write_count() == writes


def test_trash_crash_window_item_gone_has_no_undo_entry(mirror: _Harness) -> None:
    path = mirror.put("Docs/purged", b"gone")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    item = mirror.fake.find(docs, path.name)[0]
    path.unlink()
    mirror.fake.faults.add(Fault(_patch, "crash_after"))

    with pytest.raises(SimulatedCrash):
        mirror.run()
    assert mirror.fake.file(item.id).trashed
    # The trash was emptied before the next run: the item is gone for good.
    mirror.fake._state.delete(item.id)

    mirror.reopen()
    next_run = mirror.run()
    assert mirror.index.inflight_ops() == []
    assert mirror.index.trash_log(next_run.run_id) == []
    assert mirror.engine.undo().untrashed == 0


def test_executor_warnings_reach_the_run_summary() -> None:
    summary = RunSummary(7, RunResult.OK)
    report = ExecReport(Counter(), 0, [("Docs/old", "remote_changed")], None, [])
    report.warnings.append(PlanIssue("folder-kept-protected", "Docs/old"))

    Engine._record_report(summary, report)

    assert summary.result is RunResult.PARTIAL
    assert PlanIssue("folder-kept-protected", "Docs/old") in summary.warnings


@pytest.mark.parametrize("preview_first", [False, True])
def test_trash_crash_window_still_undoable(mirror: _Harness, preview_first: bool) -> None:
    path = mirror.put("Docs/crashed", b"restore")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    item = mirror.fake.find(docs, path.name)[0]
    path.unlink()
    mirror.fake.faults.add(Fault(_patch, "crash_after"))

    with pytest.raises(SimulatedCrash):
        mirror.run()
    assert mirror.fake.file(item.id).trashed

    mirror.reopen()
    if preview_first:
        mirror.engine.preview()
    next_run = mirror.run()
    assert mirror.index.trash_log(next_run.run_id) == [(item.id, "Docs/crashed", False)]

    undone = mirror.engine.undo()
    assert undone.restored_files == 1
    assert path.read_bytes() == b"restore"
    assert not mirror.fake.file(item.id).trashed


def test_undo_refuses_symlink_parent_and_traversing_remote_name(
    mirror: _Harness, tmp_path: Path
) -> None:
    path = mirror.put("Docs/sub/restore")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    sub = mirror.fake.find(docs, "sub")[0].id
    mirror.fake.add_file("../escape", sub, b"unsafe")
    path.unlink()
    path.parent.rmdir()
    mirror.run()
    outside = tmp_path / "outside"
    outside.mkdir()
    anchor = mirror.root / "Docs"
    anchor.rename(mirror.root / "Saved")
    anchor.symlink_to(outside, target_is_directory=True)

    summary = mirror.engine.undo()

    assert summary.restored_files == 0
    assert summary.failed == [("Docs/sub", "unsafe_restore_path")]
    assert list(outside.iterdir()) == []

    anchor.unlink()
    (mirror.root / "Saved").rename(anchor)
    summary = mirror.engine.undo()
    assert summary.restored_files == 1
    assert any(code == "unsafe_restore_path" for _, code in summary.failed)
    assert not (anchor / "escape").exists()


def test_unexpected_error_is_logged_and_finishes_run(
    mirror: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_network() -> bool:
        raise RuntimeError("test internal error")

    mirror.deps.network_check = broken_network
    with caplog.at_level(logging.ERROR, logger="nimbo.engine"):
        summary = mirror.run(result=RunResult.FAILED)
    assert summary.reason == "internal_error"
    assert any(record.exc_info for record in caplog.records)
    assert not mirror.paths.progress_file.exists()
    assert mirror.deps.notifier.calls == [(summary, True)]


def test_nfc_collision_is_partial_and_keeps_remote_copies(mirror: _Harness) -> None:
    composed = "Docs/Café"
    decomposed = "Docs/Cafe\u0301"
    mirror.put(composed, b"original")
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    original = mirror.fake.find(docs, "Café")[0].id
    other = mirror.fake.add_file("Cafe\u0301", docs, b"second copy")
    mirror.put(decomposed, b"different version")

    summary = mirror.run(result=RunResult.PARTIAL)

    assert {w.rel for w in summary.warnings if w.code == "nfc-collision"} == {composed, decomposed}
    assert summary.updated == summary.uploaded == summary.trashed == 0
    assert mirror.fake.file(original).content == b"original"
    assert mirror.fake.file(other).content == b"second copy"
    assert not mirror.fake.file(original).trashed and not mirror.fake.file(other).trashed
    assert {p.rel for p in mirror.engine.verify().scan_problems} == {composed, decomposed}


@pytest.mark.parametrize("blocked", ["Docs/hidden", "Docs"])
def test_unreadable_scan_is_partial_and_auditable(
    mirror: _Harness, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    mirror.put("Docs/hidden/report", b"previous")
    mirror.run()
    initial = mirror.fake.snapshot(mirror.target)
    last_ok = mirror.index.folder_stats()["Docs"].last_ok
    mirror.clock.now += timedelta(minutes=1)
    mirror.put("Docs/hidden/report", b"new data")
    scandir = os.scandir

    def blocked_scandir(path):
        if path == mirror.root / blocked:
            raise PermissionError(13, "Permission denied")
        return scandir(path)

    monkeypatch.setattr("nimbo.core.scanner.os.scandir", blocked_scandir)
    summary = mirror.run(result=RunResult.PARTIAL)
    assert any(w.rel == blocked and w.code == "unreadable" for w in summary.warnings)
    assert mirror.fake.snapshot(mirror.target) == initial
    assert mirror.index.folder_stats()["Docs"].last_ok == last_ok
    assert [p.rel for p in mirror.engine.verify().scan_problems] == [blocked]


def test_scan_with_invalid_name_can_persist_partial_summary(mirror: _Harness) -> None:
    os.mkdir(os.fsencode(mirror.root / "Docs") + b"/bad\xff")
    summary = mirror.run(result=RunResult.PARTIAL)
    assert [(w.code, w.rel) for w in summary.warnings] == [("invalid-name", r"Docs/bad\udcff")]


def test_undo_descends_into_existing_folders_and_keeps_local_files(mirror: _Harness) -> None:
    missing = mirror.put("Docs/old/deeper/missing", b"restore")
    kept = mirror.put("Docs/old/deeper/kept", b"old contents")
    mirror.run()
    missing.unlink()
    kept.unlink()
    missing.parent.rmdir()
    missing.parent.parent.rmdir()
    deleted = mirror.run()
    mirror.put("Docs/old/deeper/kept", b"local contents")
    mirror.put("Docs/old/new", b"local addition")

    summary = mirror.engine.undo(deleted.run_id)

    assert summary.failed == []
    assert summary.skipped_existing == ["Docs/old/deeper/kept"]
    assert (summary.untrashed, summary.restored_files) == (1, 1)
    assert missing.read_bytes() == b"restore"
    assert kept.read_bytes() == b"local contents"
    assert mirror.run().trashed == 0


@pytest.mark.parametrize("directory", [False, True])
def test_approval_rejects_replaced_folder_descendant_id(mirror: _Harness, directory: bool) -> None:
    mirror.deps.cfg = replace(mirror.deps.cfg, safety=SafetyConfig(max_deletes=0))
    deleted = mirror.put("Docs/old/approved", b"old")
    (deleted.parent / "empty").mkdir()
    mirror.run()
    docs = mirror.fake.find(mirror.target, "Docs")[0].id
    folder = mirror.fake.find(docs, "old")[0].id
    name = "empty" if directory else "approved"
    approved_id = mirror.fake.find(folder, name)[0].id
    (deleted.parent / "empty").rmdir()
    deleted.unlink()
    deleted.parent.rmdir()
    awaiting = mirror.run(result=RunResult.NEEDS_CONFIRMATION)
    assert awaiting.fingerprint is not None
    write_approval(mirror.paths.approval_file, awaiting.fingerprint, now=mirror.clock())

    outside = mirror.fake.add_folder("Outside")
    mirror.deps.drive.update_metadata(approved_id, add_parent=outside, remove_parents=[folder])
    new_id = (
        mirror.fake.add_folder(name, folder)
        if directory
        else mirror.fake.add_file(name, folder, b"new")
    )

    assert mirror.engine.preview().brake.fingerprint != awaiting.fingerprint
    waiting_again = mirror.run(result=RunResult.NEEDS_CONFIRMATION)
    assert waiting_again.fingerprint != awaiting.fingerprint
    assert waiting_again.trashed == 0
    assert not mirror.fake.file(folder).trashed and not mirror.fake.file(new_id).trashed

    assert waiting_again.fingerprint is not None
    write_approval(mirror.paths.approval_file, waiting_again.fingerprint, now=mirror.clock())
    assert mirror.run().trashed == 1
    assert mirror.fake.file(new_id).trashed
