"""Remote index convergence and cursor safety against the in-memory Drive API."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nimbo.core.drive import LIST_GROUP_MAX, DriveClient
from nimbo.core.errors import DriveError, NimboError, ServerError, Stopped
from nimbo.core.index import (
    META_CHANGES_PROBE,
    META_CHANGES_RELIABLE,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    META_LAST_FULL,
    Index,
)
from nimbo.core.model import ChangesPage, RemoteFile
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.remote_sync import RefreshResult, RemoteIndexer
from tests.fakedrive import FakeDrive, Fault

_FILES = "/drive/v3/files"
_CHANGES = "/drive/v3/changes"
_START = f"{_CHANGES}/startPageToken"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _TokenProvider:
    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def token(self) -> str:
        return self._access_token

    def invalidate(self) -> None:
        pytest.fail("The fake should accept the test token")


class _Harness:
    def __init__(self, fake: FakeDrive, drive: DriveClient, db: Index) -> None:
        self.fake = fake
        self.drive = drive
        self.db = db
        self.now = _NOW
        self.root = fake.add_folder("mirror")
        self.outside = fake.add_folder("outside")

    def indexer(
        self,
        *,
        root: str | None = None,
        workers: int = 1,
        full_every: timedelta = timedelta(days=7),
        on_progress: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RemoteIndexer:
        return RemoteIndexer(
            self.drive,
            self.db,
            self.root if root is None else root,
            clock=lambda: self.now,
            full_every=full_every,
            workers=workers,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    def move(self, file_id: str, parent: str) -> None:
        old_parent = self.fake.file(file_id).parent_id
        assert old_parent is not None
        self.drive.update_metadata(file_id, add_parent=parent, remove_parents=[old_parent])

    def rows(self) -> list[tuple[str, RemoteFile]]:
        return list(self.db.load_tree(self.root).walk())

    def paths(self) -> dict[str, str]:
        return {path: item.id for path, item in self.rows()}


@contextmanager
def _harness(path: Path) -> Iterator[_Harness]:
    fake = FakeDrive(clock=_NOW.timestamp)
    with fake.client() as http, closing(Index(path)) as db:
        drive = DriveClient(
            http,
            _TokenProvider(fake.oauth.add_access_token("test-token")),
            RateLimiter(reads_per_second=10_000, writes_per_second=10_000),
            RetryPolicy(max_attempts=1),
        )
        yield _Harness(fake, drive, db)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    """Wire a real Drive client, fake HTTP transport and private SQLite index."""
    with _harness(tmp_path / "index.sqlite") as subject:
        yield subject


def _paginate_changes(monkeypatch: pytest.MonkeyPatch, drive: DriveClient) -> None:
    changes = drive.changes
    monkeypatch.setattr(drive, "changes", lambda token: changes(token, page_size=1))


def test_full_listing_deep_tree_and_pagination(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List wide and deep trees with paginated groups and overlapping requests."""
    fake = harness.fake
    folders = [fake.add_folder(f"group-{n}", harness.root) for n in range(95)]
    branch = folders[-1]
    for depth in range(12):
        branch = fake.add_folder(f"depth-{depth}", branch)
    leaf = fake.add_file("leaf", branch, b"end")
    for n in range(1001):
        fake.add_file(f"file-{n}", folders[0], b"payload")

    # Block before entering the fake transport, which serializes request handling.
    parallel = Barrier(2, timeout=5)
    list_children = harness.drive.list_children

    def list_group(ids: list[str]) -> Iterator[RemoteFile]:
        if len(ids) == LIST_GROUP_MAX:
            parallel.wait()
        return list_children(ids)

    monkeypatch.setattr(harness.drive, "list_children", list_group)
    progress: list[int] = []
    result = harness.indexer(workers=2, on_progress=progress.append).refresh()
    assert result == RefreshResult("full", 1 + 95 + 12 + 1 + 1001, 0)
    assert harness.db.remote_count() == result.items
    assert harness.db.get_remote(leaf) is not None
    assert len(harness.rows()) == result.items - 1
    assert [call.path for call in fake.calls[:2]] == [_START, f"{_FILES}/{harness.root}"]
    listings = [call.query for call in fake.calls if call.path == _FILES]
    assert any(query.get("pageToken") for query in listings)
    assert sorted(query["q"].count(" in parents") for query in listings) == [1] * 13 + [
        15,
        40,
        40,
        40,
    ]
    assert progress == sorted(progress)
    assert progress[-1] == result.items
    assert harness.db.get_meta(META_LAST_FULL) == _NOW.isoformat()


def test_full_listing_ignores_trashed_and_includes_natives(harness: _Harness) -> None:
    """Keep native and shortcut rows, while skipping trashed subtrees."""
    fake = harness.fake
    folder = fake.add_folder("folder", harness.root)
    doc = fake.add_native("document", folder)
    shortcut = fake.add_native("shortcut", folder, "application/vnd.google-apps.shortcut")
    trashed = fake.add_folder("trashed", folder)
    fake.add_file("hidden", trashed, b"")
    harness.drive.update_metadata(trashed, trashed=True)
    assert harness.indexer().full_listing() == 4
    assert harness.paths() == {
        "folder": folder,
        "folder/document": doc,
        "folder/shortcut": shortcut,
    }
    assert all(item.is_google_native for _, item in harness.rows() if item.id in {doc, shortcut})


@pytest.mark.parametrize("invalid", ["missing", "trashed", "file"])
def test_full_listing_target_missing(harness: _Harness, invalid: str) -> None:
    """Reject invalid target folders without replacing the existing checkpoint."""
    harness.indexer().refresh()
    token = harness.db.get_meta(META_CHANGES_TOKEN)
    root = harness.root
    if invalid == "trashed":
        harness.drive.update_metadata(root, trashed=True)
    elif invalid == "file":
        root = harness.fake.add_file("file", harness.outside, b"")
    else:
        root = "absent-target"
    with pytest.raises(NimboError) as error:
        harness.indexer(root=root).full_listing()
    assert error.value.code == "target_missing"
    assert harness.db.get_meta(META_CHANGES_TOKEN) == token
    assert harness.db.get_remote(harness.root) is not None


def test_token_taken_before_listing_catches_concurrent_change(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item created after a list response remains visible from the saved cursor."""
    drive = harness.drive
    list_children = drive.list_children

    def list_then_create(ids: list[str]) -> Iterator[RemoteFile]:
        yield from list_children(ids)
        harness.fake.add_file("concurrent", harness.root, b"data")

    with monkeypatch.context() as patch:
        patch.setattr(drive, "list_children", list_then_create)
        assert harness.indexer().full_listing() == 1
    assert harness.paths() == {}
    assert harness.indexer().refresh() == RefreshResult("changes", 2, 1)
    assert set(harness.paths()) == {"concurrent"}


def test_changes_create_modify_rename(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Consume every page and keep the final name, checksum and cursor."""
    harness.indexer().full_listing()
    original_token = harness.db.get_meta(META_CHANGES_TOKEN)
    harness.fake.calls.clear()
    _paginate_changes(monkeypatch, harness.drive)
    file_id = harness.fake.add_file("original", harness.root, b"x")
    harness.fake.set_content(file_id, b"updated data")
    harness.drive.update_metadata(file_id, name="renamed")
    progress: list[int] = []
    assert harness.indexer(on_progress=progress.append).refresh() == RefreshResult("changes", 2, 3)
    assert harness.paths() == {"renamed": file_id}
    assert harness.db.get_remote(file_id) == harness.drive.get(file_id)
    assert sum(call.path == _CHANGES for call in harness.fake.calls) == 3
    assert progress == [1, 2, 3]
    assert harness.db.get_meta(META_CHANGES_TOKEN) != original_token
    assert harness.db.get_meta(META_CHANGES_TOKEN) == harness.drive.start_page_token()
    assert harness.indexer().apply_changes() == 0


def test_changes_move_in_lists_subtree(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve child-before-parent changes across pages and list unchanged descendants."""
    fake, drive = harness.fake, harness.drive
    incoming = fake.add_folder("incoming", harness.outside)
    nested = fake.add_folder("nested", incoming)
    old = fake.add_file("old", nested, b"before the cursor")
    file_id = fake.add_file("draft", nested, b"data")
    harness.indexer().full_listing()
    _paginate_changes(monkeypatch, drive)
    drive.update_metadata(file_id, name="renamed")
    harness.move(incoming, harness.root)
    fake.add_native("unrelated", harness.outside)
    assert harness.indexer().refresh() == RefreshResult("changes", 5, 2)
    assert harness.paths() == {
        "incoming": incoming,
        "incoming/nested": nested,
        "incoming/nested/old": old,
        "incoming/nested/renamed": file_id,
    }
    assert any(call.path == _FILES and incoming in call.query["q"] for call in fake.calls)


def test_changes_move_out_and_trash_and_delete(harness: _Harness) -> None:
    """Remove departed subtrees and preserve a descendant that moves to a live parent."""
    fake, drive = harness.fake, harness.drive
    moving_folder = fake.add_folder("moving", harness.root)
    moving_child = fake.add_folder("moving-child", moving_folder)
    lost = fake.add_file("lost", moving_child, b"")
    survivor = fake.add_file("survivor", moving_child, b"")
    trash = fake.add_folder("trash", harness.root)
    trash_child = fake.add_file("child", trash, b"")
    deleted = fake.add_file("deleted", harness.root, b"")
    harness.indexer().full_listing()
    harness.move(moving_folder, harness.outside)
    harness.move(survivor, harness.root)
    drive.update_metadata(trash, trashed=True)
    with fake.client() as http:
        access = fake.oauth.add_access_token("delete-token")
        http.delete(
            f"{_FILES}/{deleted}", headers={"Authorization": f"Bearer {access}"}
        ).raise_for_status()
    assert harness.indexer().apply_changes() == 5
    assert harness.paths() == {"survivor": survivor}
    assert harness.db.remote_count() == 2
    for file_id in (moving_folder, moving_child, lost, trash, trash_child, deleted):
        assert harness.db.get_remote(file_id) is None


def test_changes_untrash_restores(harness: _Harness) -> None:
    """Restore an untrashed folder without resurrecting explicitly trashed children."""
    fake, drive = harness.fake, harness.drive
    folder = fake.add_folder("folder", harness.root)
    live = fake.add_file("child", folder, b"")
    hidden = fake.add_file("hidden", folder, b"")
    drive.update_metadata(hidden, trashed=True)
    drive.update_metadata(folder, trashed=True)
    harness.indexer().full_listing()
    drive.update_metadata(folder, trashed=False)
    assert harness.indexer().refresh() == RefreshResult("changes", 3, 2)
    assert harness.paths() == {"folder": folder, "folder/child": live}
    assert harness.db.get_remote(hidden) is None


def test_changes_revisits_folder_trashed_and_restored_during_previous_listing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a descendant hidden during a listing even without child feed events."""
    folder = harness.fake.add_folder("folder", harness.root)
    child = harness.fake.add_file("child", folder, b"before the checkpoint")
    list_children = harness.drive.list_children
    changes = harness.drive.changes

    def list_and_trash(ids: list[str]) -> Iterator[RemoteFile]:
        yield from list_children(ids)
        if harness.root in ids:
            harness.drive.update_metadata(folder, trashed=True)

    def folder_changes(token: str) -> ChangesPage:
        page = changes(token)
        return ChangesPage(
            tuple(change for change in page.changes if change.file_id != child),
            page.next_page_token,
            page.new_start_page_token,
        )

    with monkeypatch.context() as patch:
        patch.setattr(harness.drive, "list_children", list_and_trash)
        assert harness.indexer().full_listing() == 2
    harness.drive.update_metadata(folder, trashed=False)
    monkeypatch.setattr(harness.drive, "changes", folder_changes)
    assert harness.indexer().refresh() == RefreshResult("changes", 3, 2)
    assert harness.paths() == {"folder": folder, "folder/child": child}


@pytest.mark.parametrize("status", [400, 404, 410])
def test_invalid_token_falls_back_to_full(harness: _Harness, status: int) -> None:
    """Replace the checkpoint if the changes endpoint rejects its token."""
    harness.indexer().refresh()
    harness.fake.add_file("new", harness.root, b"")
    harness.fake.calls.clear()
    harness.fake.faults.add(
        Fault(lambda request: request.url.path == _CHANGES, "status", status=status)
    )
    assert harness.indexer().refresh() == RefreshResult("full", 2, 0)
    assert [(call.path, call.status) for call in harness.fake.calls[:2]] == [
        (_CHANGES, status),
        (_START, 200),
    ]
    assert harness.db.get_meta(META_CHANGES_TOKEN) == harness.drive.start_page_token()
    assert set(harness.paths()) == {"new"}


def test_full_every_threshold(harness: _Harness) -> None:
    """Use changes under the age limit and take a new full snapshot at the limit."""
    interval = timedelta(hours=2)
    indexer = harness.indexer(full_every=interval)
    assert indexer.refresh().mode == "full"
    harness.now = _NOW + interval - timedelta(microseconds=1)
    assert indexer.refresh().mode == "changes"
    harness.now = _NOW + interval
    assert indexer.refresh().mode == "full"
    assert harness.db.get_meta(META_LAST_FULL) == harness.now.isoformat()
    assert indexer.refresh().mode == "changes"


@pytest.mark.parametrize("last_full", [None, "bad timestamp", "2026-01-01T00:00:00"])
def test_unusable_full_timestamp_causes_listing(harness: _Harness, last_full: str | None) -> None:
    """Fall back safely when a prior timestamp is missing or cannot be compared."""
    harness.indexer().refresh()
    harness.db.set_meta(META_LAST_FULL, last_full)
    assert harness.indexer().refresh().mode == "full"


def test_force_full_meta(harness: _Harness) -> None:
    """Honor the persistent flag and clear it with the new full checkpoint."""
    indexer = harness.indexer()
    indexer.refresh()
    harness.db.set_meta(META_FORCE_FULL, "1")
    assert indexer.refresh().mode == "full"
    assert harness.db.get_meta(META_FORCE_FULL) is None
    assert indexer.refresh(force_full=True).mode == "full"
    assert indexer.refresh().mode == "changes"


@pytest.mark.parametrize("age", [timedelta(minutes=10), timedelta(minutes=11)])
def test_probe_reliable_and_unreliable_paths(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, age: timedelta
) -> None:
    """Test both probe outcomes even when local writes already indexed the probe rows."""
    indexer, fake, db = harness.indexer(), harness.fake, harness.db
    indexer.full_listing()
    actual = fake.add_file("observed", harness.root, b"")
    indexer.record_probe(["other", actual, *(f"unused-{n}" for n in range(25))])
    assert json.loads(db.get_meta(META_CHANGES_PROBE) or "null") == {
        "ids": ["other", actual, *(f"unused-{n}" for n in range(18))],
        "recorded_at": harness.now.isoformat(),
    }
    assert indexer.refresh() == RefreshResult("changes", 2, 1)
    assert db.get_meta(META_CHANGES_RELIABLE) == "1"
    assert db.get_meta(META_CHANGES_PROBE) is None

    # Simulate a successful executor upsert and a feed that omits this creation.
    omitted = fake.add_file("omitted", harness.root, b"")
    remote = harness.drive.get(omitted)
    assert remote is not None
    db.upsert_remote([remote])
    indexer.record_probe([omitted])
    fake.add_file("also omitted", harness.root, b"")
    changes = harness.drive.changes

    def omit_events(token: str) -> ChangesPage:
        page = changes(token)
        return ChangesPage((), page.next_page_token, page.new_start_page_token)

    monkeypatch.setattr(harness.drive, "changes", omit_events)
    harness.now += age
    assert indexer.refresh() == RefreshResult("full", 4, 0)
    assert db.get_meta(META_CHANGES_RELIABLE) == "0"
    assert db.get_meta(META_CHANGES_PROBE) is None
    fake.calls.clear()
    assert indexer.refresh().mode == "full"
    assert all(call.path != _CHANGES for call in fake.calls)


@pytest.mark.parametrize("reliability", [None, "1"])
def test_young_probe_survives_full_listing_until_reported(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, reliability: str | None
) -> None:
    """Use the old feed window to retry the probe after a full listing during the grace period."""
    indexer, fake, db = harness.indexer(), harness.fake, harness.db
    indexer.full_listing()
    token = db.get_meta(META_CHANGES_TOKEN)
    db.set_meta(META_CHANGES_RELIABLE, reliability)
    harness.now += timedelta(hours=1)
    file_id = fake.add_file("delayed", harness.root, b"")
    indexer.record_probe([file_id])
    probe = db.get_meta(META_CHANGES_PROBE)
    assert probe is not None
    harness.now += timedelta(minutes=10) - timedelta(microseconds=1)
    changes = harness.drive.changes

    def delay_event(token: str) -> ChangesPage:
        page = changes(token)
        return ChangesPage((), page.next_page_token, page.new_start_page_token)

    # The next cursor from Drive already excludes the delayed event.
    with monkeypatch.context() as patch:
        patch.setattr(harness.drive, "changes", delay_event)
        assert indexer.refresh() == RefreshResult("full", 2, 0)
    assert harness.paths() == {"delayed": file_id}
    assert db.get_meta(META_CHANGES_TOKEN) == token
    assert db.get_meta(META_CHANGES_PROBE) == probe
    assert db.get_meta(META_CHANGES_RELIABLE) == reliability

    indexer.record_probe([])
    assert db.get_meta(META_CHANGES_PROBE) == probe
    assert harness.indexer().refresh() == RefreshResult("changes", 2, 1)
    assert db.get_meta(META_CHANGES_RELIABLE) == "1"
    assert db.get_meta(META_CHANGES_PROBE) is None


@pytest.mark.parametrize(
    ("reported", "reliability"), [(False, "1"), (True, None)], ids=["missing", "reported"]
)
def test_probe_recorded_in_future_uses_new_full_checkpoint(
    harness: _Harness, reported: bool, reliability: str | None
) -> None:
    """Discard a probe after the clock moves back past its recording time."""
    indexer, fake, db = harness.indexer(), harness.fake, harness.db
    indexer.full_listing()
    token = db.get_meta(META_CHANGES_TOKEN)
    db.set_meta(META_CHANGES_RELIABLE, reliability)
    harness.now += timedelta(minutes=2)
    file_id = fake.add_file("created", harness.root, b"")
    indexer.record_probe([file_id if reported else "omitted"])
    harness.now -= timedelta(minutes=1)
    new_token = harness.drive.start_page_token()
    assert new_token != token
    fake.calls.clear()

    assert indexer.refresh() == RefreshResult("full", 2, 0)
    assert [call.path for call in fake.calls[:2]] == [_CHANGES, _START]
    assert db.get_meta(META_CHANGES_TOKEN) == new_token
    assert db.get_meta(META_CHANGES_PROBE) is None
    assert db.get_meta(META_CHANGES_RELIABLE) == reliability
    assert harness.paths() == {"created": file_id}
    assert indexer.refresh() == RefreshResult("changes", 2, 0)


@pytest.mark.parametrize("reported", [False, True])
def test_legacy_probe_has_no_grace_period(harness: _Harness, reported: bool) -> None:
    """Read a legacy list and immediately record whether the feed contains one of its IDs."""
    indexer, fake, db = harness.indexer(), harness.fake, harness.db
    indexer.full_listing()
    file_id = fake.add_file("created", harness.root, b"")
    db.set_meta(META_CHANGES_PROBE, json.dumps([file_id if reported else "omitted"]))

    assert indexer.refresh() == (
        RefreshResult("changes", 2, 1) if reported else RefreshResult("full", 2, 0)
    )
    assert db.get_meta(META_CHANGES_RELIABLE) == ("1" if reported else "0")
    assert db.get_meta(META_CHANGES_PROBE) is None


def test_full_checkpoint_consumes_superseded_probe(harness: _Harness) -> None:
    """Avoid interpreting a cursor deliberately advanced by full listing as missing changes."""
    indexer = harness.indexer()
    indexer.full_listing()
    file_id = harness.fake.add_file("created", harness.root, b"")
    indexer.record_probe([file_id])
    indexer.refresh(force_full=True)
    assert harness.db.get_meta(META_CHANGES_PROBE) is None
    assert indexer.refresh() == RefreshResult("changes", 2, 0)
    assert harness.db.get_meta(META_CHANGES_RELIABLE) is None


@pytest.mark.parametrize("mode", ["changes", "full", "probe"])
def test_stop_raises_stopped(harness: _Harness, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Cancellation during either walk preserves all rows and checkpoint metadata."""
    indexer = harness.indexer()
    indexer.full_listing()
    db, fake = harness.db, harness.fake
    previous = db.get_meta(META_CHANGES_TOKEN)
    folder = fake.add_folder("new", harness.root)
    fake.add_file("child", folder, b"")
    _paginate_changes(monkeypatch, harness.drive)
    stop = False

    def progress(items: int) -> None:
        nonlocal stop
        if mode != "probe" or any(call.path == f"{_FILES}/{harness.root}" for call in fake.calls):
            stop = True

    fake.calls.clear()
    if mode == "probe":
        indexer.record_probe(["omitted"])
    probe = db.get_meta(META_CHANGES_PROBE)
    if mode == "full":
        db.set_meta(META_FORCE_FULL, "1")
    with pytest.raises(Stopped):
        harness.indexer(should_stop=lambda: stop, on_progress=progress).refresh()
    assert db.get_meta(META_CHANGES_TOKEN) == previous
    assert db.get_meta(META_LAST_FULL) == _NOW.isoformat()
    assert db.get_meta(META_FORCE_FULL) == ("1" if mode == "full" else None)
    assert db.get_meta(META_CHANGES_PROBE) == probe
    assert db.get_meta(META_CHANGES_RELIABLE) is None
    assert harness.paths() == {}
    assert db.remote_count() == 1
    if mode == "changes":
        assert [call.path for call in fake.calls] == [_CHANGES]


@pytest.mark.parametrize("status", [400, 404, 503])
def test_subtree_list_failure_preserves_checkpoint(harness: _Harness, status: int) -> None:
    """A failed subtree request leaves old rows and cursor available for a retry."""
    fake, db = harness.fake, harness.db
    incoming = fake.add_folder("incoming", harness.outside)
    child = fake.add_file("child", incoming, b"")
    indexer = harness.indexer()
    indexer.refresh()
    token = db.get_meta(META_CHANGES_TOKEN)
    harness.move(incoming, harness.root)
    fake.faults.add(Fault(lambda request: request.url.path == _FILES, "status", status=status))
    with pytest.raises(DriveError) as error:
        indexer.refresh()
    assert error.value.status == status
    assert harness.paths() == {}
    assert db.get_meta(META_CHANGES_TOKEN) == token
    assert indexer.refresh() == RefreshResult("changes", 3, 1)
    assert harness.paths()["incoming/child"] == child


def test_later_change_page_failure_preserves_checkpoint(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable error on a later page leaves all pages available on retry."""
    indexer = harness.indexer()
    indexer.full_listing()
    token = harness.db.get_meta(META_CHANGES_TOKEN)
    _paginate_changes(monkeypatch, harness.drive)
    for n in range(3):
        harness.fake.add_file(str(n), harness.root, b"")
    harness.fake.faults.add(
        Fault(lambda req: req.url.path == _CHANGES, "status", status=503, skip=1)
    )
    with pytest.raises(ServerError):
        indexer.refresh()
    assert harness.db.get_meta(META_CHANGES_TOKEN) == token
    assert harness.paths() == {}
    assert indexer.refresh() == RefreshResult("changes", 4, 3)


@pytest.mark.parametrize("broken", ["missing cursor", "looping cursor"])
def test_malformed_changes_does_not_advance_cursor(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """Require a complete feed and a final checkpoint before writing any delta."""
    indexer = harness.indexer()
    indexer.full_listing()
    token = harness.db.get_meta(META_CHANGES_TOKEN)
    harness.fake.add_file("new", harness.root, b"")
    page = harness.drive.changes(token or "")
    bad_page = ChangesPage(page.changes, token if broken == "looping cursor" else None, None)
    monkeypatch.setattr(harness.drive, "changes", lambda _: bad_page)
    with pytest.raises(DriveError):
        indexer.apply_changes()
    assert harness.db.get_meta(META_CHANGES_TOKEN) == token
    assert harness.paths() == {}


@pytest.mark.parametrize("mode", ["full", "changes"])
def test_refresh_rolls_back_rows_and_cursor_on_transaction_failure(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Inject a failure after SQL has written the new cursor and verify rollback."""
    db = harness.db
    indexer = harness.indexer()
    indexer.refresh()
    before = db.get_meta(META_CHANGES_TOKEN)
    harness.fake.add_file("new", harness.root, b"")
    set_meta = db._set_meta

    def fail_after_setting_meta(key: str, value: str | None) -> None:
        set_meta(key, value)
        if key == META_CHANGES_TOKEN:
            raise sqlite3.OperationalError("injected transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_set_meta", fail_after_setting_meta)
        with pytest.raises(sqlite3.OperationalError, match="injected transaction failure"):
            indexer.refresh(force_full=mode == "full")
    assert db.get_meta(META_CHANGES_TOKEN) == before
    assert harness.paths() == {}
    assert indexer.refresh(force_full=mode == "full").items == 2


def test_changes_target_disappears(harness: _Harness) -> None:
    """A target trash change must fail before an empty mirror reaches the planner."""
    indexer = harness.indexer()
    harness.fake.add_file("child", harness.root, b"")
    indexer.full_listing()
    token = harness.db.get_meta(META_CHANGES_TOKEN)
    harness.drive.update_metadata(harness.root, trashed=True)
    with pytest.raises(NimboError) as error:
        indexer.refresh()
    assert error.value.code == "target_missing"
    assert harness.db.get_meta(META_CHANGES_TOKEN) == token
    assert set(harness.paths()) == {"child"}


def test_root_can_move_under_a_folder_that_left_the_mirror(harness: _Harness) -> None:
    """Keep the target and its contents when a former child becomes its parent."""
    fake, db = harness.fake, harness.db
    former_child = fake.add_folder("former-child", harness.root)
    file_id = fake.add_file("retained", harness.root, b"data")
    indexer = harness.indexer()
    indexer.full_listing()

    harness.move(former_child, harness.outside)
    harness.move(harness.root, former_child)
    assert indexer.refresh() == RefreshResult("full", 2, 0)
    assert harness.paths() == {"retained": file_id}
    assert db.remote_count() == 2
    assert db.get_remote(former_child) is None
    assert db.get_remote(harness.root) == harness.drive.get(harness.root)
    assert db.get_meta(META_CHANGES_TOKEN) == harness.drive.start_page_token()


def test_refresh_repairs_unreachable_reconciliation_rows(harness: _Harness) -> None:
    """Replace a checkpoint whose reconciliation added a row outside the target."""
    indexer = harness.indexer()
    indexer.full_listing()
    file_id = harness.fake.add_file("outside", harness.outside, b"")
    item = harness.drive.get(file_id)
    assert item is not None
    harness.db.upsert_remote([item])

    assert indexer.refresh() == RefreshResult("full", 1, 0)
    assert harness.db.get_remote(file_id) is None
    assert harness.db.get_meta(META_CHANGES_TOKEN) == harness.drive.start_page_token()


_Action = tuple[str, int, int, str, bytes]
_ACTIONS = st.tuples(
    st.sampled_from(
        ("file", "folder", "rename", "move", "in", "out", "trash", "untrash", "edit", "sync")
    ),
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=0, max_value=99),
    st.sampled_from(("same", "renamed", "café", "cafe\u0301", "odd/name")),
    st.binary(max_size=12),
)


def _mutate(harness: _Harness, ids: list[str], action: _Action) -> None:
    fake, drive = harness.fake, harness.drive
    kind, which, where, name, data = action
    files = [fake.file(file_id) for file_id in ids]
    if kind == "untrash":
        trashed = [item for item in files if item.explicitly_trashed]
        if trashed:
            drive.update_metadata(trashed[which % len(trashed)].id, trashed=False)
        return

    active = [item for item in files if not item.trashed]
    folders = [item.id for item in active if item.is_folder]
    parents = [harness.root, harness.outside, *folders]
    parent = parents[where % len(parents)]
    if kind == "file":
        ids.append(fake.add_file(name, parent, data))
    elif kind == "folder":
        ids.append(fake.add_folder(name, parent))
    elif kind == "edit":
        binary = [item for item in active if item.is_binary]
        if binary:
            fake.set_content(binary[which % len(binary)].id, data, keep_modified=where % 2 == 0)
    elif active:
        item = active[which % len(active)]
        if kind == "rename":
            drive.update_metadata(item.id, name=name)
        elif kind == "trash":
            drive.update_metadata(item.id, trashed=True)
        else:
            if kind == "in":
                parent = harness.root
            elif kind == "out":
                parent = harness.outside
            ancestor = parent
            while ancestor not in (harness.root, harness.outside):
                if ancestor == item.id:
                    return
                ancestor = fake.file(ancestor).parent_id or harness.outside
            if parent != item.parent_id:
                harness.move(item.id, parent)


@given(actions=st.lists(_ACTIONS, min_size=1, max_size=45))
@example(actions=[("in", 2, 0, "", b""), ("sync", 0, 0, "", b""), ("out", 2, 0, "", b"")])
@example(actions=[("trash", 0, 0, "", b""), ("untrash", 0, 0, "", b"")])
def test_changes_equivalent_to_full_listing(actions: list[_Action]) -> None:
    """Check full metadata and reachability after arbitrary batches and cursor resumes."""
    with TemporaryDirectory() as directory:
        path = Path(directory)
        with (
            _harness(path / "incremental.sqlite") as harness,
            closing(Index(path / "full.sqlite")) as full,
        ):
            fake = harness.fake
            inside = fake.add_folder("inside-folder", harness.root)
            internal_file = fake.add_file("inside-file", inside, b"initial")
            outside = fake.add_folder("outside-folder", harness.outside)
            nested = fake.add_folder("nested", outside)
            outside_file = fake.add_file("outside-file", nested, b"initial")
            fake.add_native("native", nested)
            ids = [inside, internal_file, outside, nested, outside_file]
            indexer = harness.indexer(workers=2)
            fresh = RemoteIndexer(harness.drive, full, harness.root, clock=lambda: _NOW, workers=2)
            indexer.full_listing()

            def compare() -> None:
                indexer.apply_changes()
                assert fresh.full_listing() == harness.db.remote_count()
                assert harness.rows() == list(full.load_tree(harness.root).walk())
                assert harness.db.get_remote(harness.root) == full.get_remote(harness.root)
                assert harness.db.get_meta(META_CHANGES_TOKEN) == full.get_meta(META_CHANGES_TOKEN)

            for action in actions:
                if action[0] == "sync":
                    compare()
                else:
                    _mutate(harness, ids, action)
            compare()
