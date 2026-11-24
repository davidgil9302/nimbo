"""SQLite index persistence, transactions, and crash recovery."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from threading import Barrier, Event

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.errors import NimboError
from nimbo.core.index import (
    _MIGRATIONS,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    SCHEMA_VERSION,
    Index,
    TrashFile,
)
from nimbo.core.model import (
    FOLDER_MIME,
    FolderStat,
    Op,
    OpKind,
    OpState,
    PlanIssue,
    RemoteFile,
    RunResult,
    RunSummary,
)
from nimbo.core.tree import RemoteTree

STARTED = "2026-01-01T00:00:00+00:00"
FINISHED = "2026-01-01T01:00:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Provide a fresh path that also needs escaping in a SQLite URI."""
    return tmp_path / "index # ?%.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Index]:
    """Provide and close a writable index."""
    with closing(Index(db_path)) as index:
        yield index


def _file(file_id: str, *parents: str, folder: bool = False) -> RemoteFile:
    return RemoteFile(file_id, file_id, FOLDER_MIME if folder else "text/plain", parents)


def test_create_sets_wal_and_private_mode(db: Index, db_path: Path) -> None:
    db.set_meta("key", "value")

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    settings = {
        "journal_mode": "wal",
        "synchronous": 2,
        "foreign_keys": 1,
        "busy_timeout": 30_000,
        "user_version": SCHEMA_VERSION,
    }
    for key, value in settings.items():
        assert db._connection.execute(f"PRAGMA {key}").fetchone()[0] == value


def test_reopen_keeps_data(db_path: Path) -> None:
    item = _file("item", "root")
    with closing(Index(db_path)) as index:
        index.replace_remote([item], meta={META_CHANGES_TOKEN: "token"})
        index.put_hash("item", 10, 20, "hash")

    db_path.chmod(0o644)
    with closing(Index(db_path)) as index:
        assert index.get_meta(META_CHANGES_TOKEN) == "token"
        assert index.get_remote("item") == item
        assert index.get_hash("item", 10, 20) == "hash"
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_db_too_new_rejected(db_path: Path) -> None:
    version = SCHEMA_VERSION + 1
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute("CREATE TABLE future (data TEXT)")

    for open_index in (Index, Index.open_readonly):
        with pytest.raises(NimboError) as caught:
            open_index(db_path)
        assert caught.value.code == "db_too_new"

    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("future",)]


def test_migration_failure_rolls_back_schema_and_version(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE op (conflict TEXT)")

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Index(db_path)

    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("op",)]


def test_migrate_v1_preserves_trash_log_without_inventing_file_snapshots(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO run (id, trigger, started_at) VALUES (1, 'manual', ?)", (STARTED,)
        )
        connection.executemany(
            "INSERT INTO trash_log VALUES (?, ?, ?, ?)",
            [(1, "file", "Photos/report", 0), (1, "folder", "Photos/old", 1)],
        )

    with closing(Index(db_path)) as index:
        assert index._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert index.last_run_with_trash() == 1
        assert index.trash_log(1) == [
            ("file", "Photos/report", False),
            ("folder", "Photos/old", True),
        ]
        assert index.trash_files(1) == {}


def test_migrate_v2_preserves_trash_snapshots(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as previous:
        previous.setattr("nimbo.core.index.SCHEMA_VERSION", 2)
        with closing(Index(db_path)) as index:
            assert index._connection.execute("PRAGMA user_version").fetchone()[0] == 2
            run_id = index.start_run("manual", STARTED)
            op = Op(OpKind.TRASH, "Photos/old", "deleted-locally", file_id="folder")
            index.save_ops(run_id, [op])
            index.record_trash_files(
                op, [TrashFile("file", "Photos/old/report", 10, "recorded-md5")]
            )
            index._connection.execute(
                "INSERT INTO trash_log (run_id, file_id, rel, is_folder, op_id) "
                "VALUES (?, ?, ?, 1, ?)",
                (run_id, op.file_id, op.rel, op.db_id),
            )

    with closing(Index(db_path)) as index:
        assert index._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert index.guarded_trash_ids(op) is None
        snapshot = index.trash_files(run_id)["file"]
        assert (snapshot.rel, snapshot.size, snapshot.md5) == (
            "Photos/old/report",
            10,
            "recorded-md5",
        )


def test_migrate_v3_refreshes_unknown_ownership(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for version in (1, 2, 3):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")
        connection.execute("INSERT INTO meta VALUES (?, 'cursor')", (META_CHANGES_TOKEN,))
        connection.execute("INSERT INTO remote (id, name, mime_type) VALUES ('id', 'old', 'text')")

    with closing(Index.open_readonly(db_path)) as index:
        assert index.get_remote("id") == RemoteFile("id", "old", "text")

    with closing(Index(db_path)) as index:
        assert index.get_remote("id") == RemoteFile("id", "old", "text")
        assert index.get_meta(META_FORCE_FULL) == "1"
        assert index.get_meta(META_CHANGES_TOKEN) == "cursor"
        for owned in (False, True, None):
            item = RemoteFile("id", "old", "text", owned_by_me=owned)
            index.upsert_remote([item])
            assert index.get_remote("id") == item


def test_meta_set_get_delete(db: Index) -> None:
    assert db.get_meta("clé") is None
    db.set_meta("clé", "")
    assert db.get_meta("clé") == ""
    db.set_meta("clé", "cafe\u0301")
    assert db.get_meta("clé") == "cafe\u0301"
    db.set_meta("clé", None)
    db.set_meta("clé", None)
    assert db.get_meta("clé") is None


def test_replace_remote_is_atomic(db: Index) -> None:
    old = _file("old")
    new = _file("new")
    db.replace_remote([old], meta={META_CHANGES_TOKEN: "old"})

    def broken_listing() -> Iterator[RemoteFile]:
        yield new
        raise RuntimeError("listing stopped")

    with pytest.raises(RuntimeError, match="listing stopped"):
        db.replace_remote(broken_listing(), meta={META_CHANGES_TOKEN: "new"})

    assert db.remote_count() == 1
    assert db.get_remote("old") == old
    assert db.get_remote("new") is None
    assert db.get_meta(META_CHANGES_TOKEN) == "old"
    db.replace_remote([new], meta={META_CHANGES_TOKEN: None})
    assert db.remote_count() == 1
    assert db.get_remote("old") is None
    assert db.get_remote("new") == new
    assert db.get_meta(META_CHANGES_TOKEN) is None


def test_delete_remote_removes_subtree(db: Index) -> None:
    survivor = _file("survivor", "root", "parent")
    db.upsert_remote(
        [
            _file("child", "parent", folder=True),
            _file("leaf", "child"),
            _file("parent", "outside"),
            _file("outside", "parent"),
            survivor,
            _file("orphan", "missing"),
        ]
    )

    db.delete_remote(["parent", "child", "missing", "parent"])

    assert db.remote_count() == 1
    assert db.get_remote("survivor") == survivor


def test_apply_remote_delta_atomic_with_meta(db: Index) -> None:
    old_parent = _file("old", "root", folder=True)
    item = _file("item", "old")
    new_parent = _file("new", "root", folder=True)
    moved = _file("item", "new")
    db.replace_remote([old_parent, item], meta={META_CHANGES_TOKEN: "old", "obsolete": "yes"})

    with pytest.raises(ValueError):
        db.apply_remote_delta(
            [new_parent, moved], ["old"], meta={META_CHANGES_TOKEN: "new", "bad": "\udcff"}
        )

    assert db.remote_count() == 2
    assert db.get_remote("old") == old_parent
    assert db.get_remote("item") == item
    assert db.get_remote("new") is None
    assert db.get_meta(META_CHANGES_TOKEN) == "old"

    db.apply_remote_delta(
        [new_parent, moved], ["old"], meta={META_CHANGES_TOKEN: "new", "obsolete": None}
    )
    assert db.remote_count() == 2
    assert db.get_remote("old") is None
    assert db.get_remote("item") == moved
    assert db.get_remote("new") == new_parent
    assert db.get_meta(META_CHANGES_TOKEN) == "new"
    assert db.get_meta("obsolete") is None


def test_load_tree_matches_inserted(db: Index) -> None:
    root = _file("root", folder=True)
    folder = _file("folder", "root", folder=True)
    item = RemoteFile("item", "cafe\u0301", "text/plain", ("folder", "root"))
    trash = RemoteFile("trash", "Trash", "text/plain", ("root",), trashed=True)
    outside = _file("outside", "missing")
    files = [trash, item, outside, folder, root]
    db.upsert_remote(files)

    actual = db.load_tree("root")
    expected = RemoteTree("root", files)
    assert list(actual.walk()) == list(expected.walk())
    assert actual.by_rel() == {"folder": [folder], "folder/café": [item]}
    assert actual.get("outside") == outside
    assert actual.get("trash") is None
    assert db.get_remote("trash") == trash
    assert db.remote_count() == len(files)


def test_in_subtree_follows_live_parent_chain_to_root(db: Index) -> None:
    db.upsert_remote(
        [
            _file("root", "above", folder=True),
            _file("above", folder=True),
            _file("folder", "root", folder=True),
            _file("file", "folder"),
            _file("other", "above"),
            RemoteFile("trash", "Trash", FOLDER_MIME, ("root",), trashed=True),
            _file("under-trash", "trash"),
        ]
    )

    assert db.in_subtree("root", "root")
    assert db.in_subtree("folder", "root")
    assert db.in_subtree("file", "root")
    assert not db.in_subtree("above", "root")
    assert not db.in_subtree("other", "root")
    assert not db.in_subtree("trash", "root")
    assert not db.in_subtree("under-trash", "root")


def test_in_subtree_rejects_unknown_missing_and_cyclic_ancestry(db: Index) -> None:
    db.upsert_remote(
        [
            _file("child", "missing"),
            _file("one", "two", folder=True),
            _file("two", "one", folder=True),
            _file("self", "self", folder=True),
        ]
    )

    assert db.in_subtree("root", "root")
    assert all(
        not db.in_subtree(file_id, "root") for file_id in ("unknown", "child", "one", "two", "self")
    )


_TEXT = st.text(max_size=15)
_INT = st.integers(min_value=-(2**63), max_value=2**63 - 1)


@given(
    st.builds(
        RemoteFile,
        id=_TEXT,
        name=_TEXT,
        mime_type=_TEXT,
        parents=st.lists(_TEXT, max_size=5).map(tuple),
        size=st.none() | _INT,
        md5=st.none() | _TEXT,
        modified_ms=st.none() | _INT,
        properties=st.dictionaries(_TEXT, _TEXT, max_size=5),
        trashed=st.booleans(),
    )
)
def test_remote_roundtrip_all_fields(item: RemoteFile) -> None:
    with TemporaryDirectory() as directory, closing(Index(Path(directory) / "index.db")) as index:
        index.upsert_remote([item])
        assert index.get_remote(item.id) == item
        row = index._connection.execute(
            "SELECT parent_id, parents, properties FROM remote"
        ).fetchone()
        assert row["parent_id"] == (item.parents[0] if item.parents else None)
        assert json.loads(row["parents"]) == list(item.parents)
        assert json.loads(row["properties"]) == item.properties


def test_surrogates_rejected_in_direct_and_json_values(db: Index) -> None:
    bad = "bad\udcff"
    for item in (
        RemoteFile("id", bad, "text/plain"),
        RemoteFile("id", "name", "text/plain", ("first", bad)),
        RemoteFile("id", "name", "text/plain", properties={bad: "value"}),
        RemoteFile("id", "name", "text/plain", properties={"key": bad}),
    ):
        with pytest.raises(ValueError):
            db.upsert_remote([_file("valid"), item])
        assert db.remote_count() == 0

    with pytest.raises(ValueError):
        db.get_meta(bad)
    run_id = db.start_run("manual", STARTED)
    with pytest.raises(ValueError):
        db.finish_run(RunSummary(run_id, RunResult.PARTIAL, warnings=[PlanIssue("warning", bad)]))
    assert db.get_run(run_id) is None


def test_hash_cache_hit_miss_on_size_or_mtime_change(db: Index) -> None:
    rel = "Photos/café"
    assert db.get_hash(rel, 15, 22) is None
    db.put_hash(rel, 15, 22, "old")
    assert db.get_hash(rel, 15, 22) == "old"
    assert db.get_hash(rel, 16, 22) is None
    assert db.get_hash(rel, 15, 23) is None
    assert db.get_hash("Photos/cafe\u0301", 15, 22) is None
    db.put_hash(rel, 16, 23, "new")
    assert db.get_hash(rel, 15, 22) is None
    assert db.get_hash(rel, 16, 23) == "new"


def test_prune_hashes(db: Index) -> None:
    for rel in ("keep", "old-a", "old-b"):
        db.put_hash(rel, 10, 20, "hash")

    keep = {"keep", *(f"absent-{i}" for i in range(1500))}
    assert db.prune_hashes(keep) == 2
    assert db.get_hash("old-a", 10, 20) is None
    assert db.get_hash("old-b", 10, 20) is None
    assert db.get_hash("keep", 10, 20) == "hash"
    assert db.prune_hashes(set()) == 1
    assert db.prune_hashes(set()) == 0


def test_run_lifecycle_and_summary_roundtrip(db: Index) -> None:
    run_id = db.start_run("manual", STARTED)
    assert db.get_run(run_id) is None
    assert db.get_run(run_id + 1) is None
    assert db.last_run() is None
    summary = RunSummary(
        run_id=run_id,
        result=RunResult.NEEDS_CONFIRMATION,
        reason="too_many_deletes",
        detail="cafe\u0301",
        trigger="manual",
        started_at=STARTED,
        finished_at=FINISHED,
        mkdirs=1,
        uploaded=2,
        uploaded_bytes=3,
        updated=4,
        touched=5,
        moved=6,
        adopted=7,
        trashed=8,
        failed=[("Photos/café", "failed")],
        warnings=[PlanIssue("duplicate-unmanaged", "Photos/雪", "detail")],
        fingerprint="fingerprint",
        trash_preview=["Photos/old\t4"],
    )
    db.finish_run(summary)

    assert db.get_run(run_id) == db.last_run() == summary
    row = db._connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    assert row["finished_at"] == FINISHED
    assert row["result"] == RunResult.NEEDS_CONFIRMATION.value
    assert RunSummary.from_dict(json.loads(row["summary"])) == summary
    with pytest.raises(ValueError):
        db.finish_run(RunSummary(None, RunResult.OK))


def test_list_runs_newest_first(db: Index) -> None:
    summaries = [RunSummary(db.start_run("timer", STARTED), RunResult.OK) for _ in range(3)]
    for summary in reversed(summaries):
        db.finish_run(summary)
    unfinished = db.start_run("timer", STARTED)

    assert db.get_run(unfinished) is None
    assert db.list_runs() == summaries[::-1]
    assert db.list_runs(limit=2) == summaries[:0:-1]
    assert db.list_runs(limit=0) == []
    assert db.last_run() == summaries[-1]


def test_ops_state_machine(db: Index) -> None:
    run_id = db.start_run("manual", STARTED)
    move = Op(
        OpKind.MOVE,
        "Photos/café",
        "renamed",
        file_id="a",
        parent_rel="",
        parent_id="root",
        src_rel="Docs/cafe\u0301",
        src_parent_id="other-parent",
        name="café",
        size=50,
        mtime_ns=100,
        md5="digest",
        affected_files=2,
    )
    update = Op(OpKind.UPDATE, "update", "changed")
    db.save_ops(run_id, [move, update])
    assert move.db_id is not None and update.db_id is not None
    assert db.op_states(run_id) == [(move, OpState.PENDING, None), (update, OpState.PENDING, None)]

    db.mark_inflight(move)
    assert db.inflight_ops() == [move]
    db.fail_op(move, "server_error")
    assert db.op_states(run_id)[0] == (move, OpState.FAILED, "server_error")
    assert db.inflight_ops() == []
    db.reset_op(move)
    assert db.op_states(run_id)[0] == (move, OpState.PENDING, None)
    db.mark_inflight(move)
    db.complete_op(move)
    db.mark_inflight(update)
    db.abandon_ops([update.db_id])
    assert db.op_states(run_id) == [(move, OpState.DONE, None), (update, OpState.ABANDONED, None)]
    assert db.inflight_ops() == []
    assert db._connection.execute("SELECT attempts FROM op ORDER BY seq").fetchall()[0][0] == 2


def test_save_ops_failure_does_not_publish_ids(db: Index) -> None:
    run_id = db.start_run("manual", STARTED)
    good = Op(OpKind.UPLOAD, "good", "new")
    bad = Op(OpKind.UPLOAD, "bad\udcff", "new")
    with pytest.raises(ValueError):
        db.save_ops(run_id, [good, bad])
    assert good.db_id is bad.db_id is None
    assert db.op_states(run_id) == []

    with pytest.raises(sqlite3.IntegrityError):
        db.save_ops(run_id + 1, [good])
    assert good.db_id is None


def test_complete_op_updates_remote_and_trash_log_atomically(db: Index) -> None:
    run_id = db.start_run("manual", STARTED)
    op = Op(OpKind.TRASH, "Photos/old", "deleted-locally", file_id="old", affected_files=1)
    old = _file("old", "root", folder=True)
    child = _file("child", "old")
    new = _file("new", "root")
    db.save_ops(run_id, [op])
    db.upsert_remote([old, child])
    db.mark_inflight(op)

    db._connection.execute(
        "CREATE TRIGGER fail_trash BEFORE INSERT ON trash_log "
        "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        db.complete_op(
            op, upsert=[new], remove_subtrees=["old"], trash_log_run=run_id, trashed_is_folder=True
        )
    assert db.get_remote("new") is None
    assert db.get_remote("old") == old
    assert db.get_remote("child") == child
    assert db.op_states(run_id) == [(op, OpState.INFLIGHT, None)]
    assert db.trash_log(run_id) == []
    assert db.last_run_with_trash() is None

    db._connection.execute("DROP TRIGGER fail_trash")
    for _ in range(2):
        db.complete_op(
            op, upsert=[new], remove_subtrees=["old"], trash_log_run=run_id, trashed_is_folder=True
        )
    assert db.remote_count() == 1
    assert db.get_remote("new") == new
    assert db.get_remote("old") is None
    assert db.get_remote("child") is None
    assert db.op_states(run_id) == [(op, OpState.DONE, None)]
    assert db.trash_log(run_id) == [("old", "Photos/old", True)]
    assert db.last_run_with_trash() == run_id


def test_complete_op_requires_journal_id_and_file_id_for_trash(db: Index) -> None:
    run_id = db.start_run("manual", STARTED)
    op = Op(OpKind.TRASH, "Photos/old", "deleted-locally")
    item = _file("old")
    db.upsert_remote([item])

    with pytest.raises(ValueError, match="operation ID"):
        db.complete_op(op, remove_subtrees=["old"])
    assert db.get_remote("old") == item

    db.save_ops(run_id, [op])
    db.mark_inflight(op)
    with pytest.raises(ValueError, match="file ID"):
        db.complete_op(op, remove_subtrees=["old"], trash_log_run=run_id)
    assert db.get_remote("old") == item
    assert db.op_states(run_id) == [(op, OpState.INFLIGHT, None)]
    assert db.trash_log(run_id) == []


@pytest.mark.parametrize(
    "disk_rel", [None, "DOC/Mas.txt", "DOC/Ma\u0301s.txt"], ids=["none", "ascii", "nfd"]
)
def test_inflight_ops_survive_reopen(db_path: Path, disk_rel: str | None) -> None:
    first = Op(OpKind.UPLOAD, "first", "new", file_id="first-id")
    second = Op(OpKind.UPDATE, "DOC/Más.txt", "changed", file_id="second-id", disk_rel=disk_rel)
    with closing(Index(db_path)) as index:
        run_id = index.start_run("manual", STARTED)
        index.save_ops(run_id, [first, second])
        index.mark_inflight(second)

    with closing(Index(db_path)) as index:
        assert index.inflight_ops() == [second]
        assert index.op_states(run_id) == [
            (first, OpState.PENDING, None),
            (second, OpState.INFLIGHT, None),
        ]


def test_sessions_crud_and_prune(db: Index) -> None:
    upload = ("café", 12, 34, "upload")
    update = ("café", 12, 34, "update")
    assert db.get_session(*upload) is None
    db.put_session(*upload, "upload-id", "https://example.invalid/old")
    db.put_session(*upload, "new-id", "https://example.invalid/new")
    db.put_session(*update, "update-id", "https://example.invalid/update")

    assert db.get_session(*upload) == ("new-id", "https://example.invalid/new")
    assert db.get_session(*update) == ("update-id", "https://example.invalid/update")
    assert db.get_session("café", 13, 34, "upload") is None
    assert db.get_session("café", 12, 35, "upload") is None
    assert db.get_session("cafe\u0301", 12, 34, "upload") is None
    db.delete_session(*update)
    assert db.get_session(*update) is None

    db._connection.execute("UPDATE upload_session SET created_at = '2020-01-01T00:00:00+00:00'")
    assert db.prune_sessions("2020-01-01T00:00:00Z") == 0
    assert db.prune_sessions("2020-01-01T00:00:00") == 0
    assert db.prune_sessions("2019-12-31T19:00:00.000001-05:00") == 1
    assert db.get_session(*upload) is None


def test_folder_stats_roundtrip(db: Index) -> None:
    photos = FolderStat("Photos/雪", STARTED, 1, 2, 3, 4, FINISHED)
    docs = FolderStat("Docs")
    assert db.folder_stats() == {}
    db.put_folder_stats([photos, docs])
    assert db.folder_stats() == {photos.folder: photos, docs.folder: docs}

    changed = FolderStat(photos.folder, files_local=5)
    db.put_folder_stats([changed])
    assert db.folder_stats() == {changed.folder: changed, docs.folder: docs}


def test_readonly_rejects_writes_and_reads_during_writer(db: Index, db_path: Path) -> None:
    old = _file("old")
    new = _file("new")
    db.replace_remote([old], meta={META_CHANGES_TOKEN: "old"})
    run_id = db.start_run("manual", STARTED)
    op = Op(OpKind.UPLOAD, "new", "new")
    db.save_ops(run_id, [op])

    entered = Event()
    resume = Event()

    def listing() -> Iterator[RemoteFile]:
        yield new
        entered.set()
        if not resume.wait(10):
            raise TimeoutError("reader did not finish")

    with ThreadPoolExecutor(max_workers=1) as pool:
        write = pool.submit(db.replace_remote, listing(), meta={META_CHANGES_TOKEN: "new"})
        try:
            assert entered.wait(10)
            with closing(Index.open_readonly(db_path)) as reader:
                assert reader.remote_count() == 1
                assert reader.get_remote("old") == old
                assert reader.get_remote("new") is None
                assert reader.get_meta(META_CHANGES_TOKEN) == "old"
                assert reader.op_states(run_id) == [(op, OpState.PENDING, None)]
                assert reader.integrity_ok()
                writes = [
                    lambda: reader.set_meta("x", None),
                    lambda: reader.replace_remote([]),
                    lambda: reader.apply_remote_delta([], []),
                    lambda: reader.upsert_remote([]),
                    lambda: reader.delete_remote([]),
                    lambda: reader.put_hash("rel", 0, 0, "hash"),
                    lambda: reader.prune_hashes(set()),
                    lambda: reader.start_run("manual", STARTED),
                    lambda: reader.finish_run(RunSummary(run_id, RunResult.OK)),
                    lambda: reader.save_ops(run_id, []),
                    lambda: reader.mark_inflight(op),
                    lambda: reader.complete_op(op),
                    lambda: reader.fail_op(op, "failed"),
                    lambda: reader.reset_op(op),
                    lambda: reader.abandon_ops([]),
                    lambda: reader.put_session("rel", 0, 0, "upload", "id", "url"),
                    lambda: reader.delete_session("rel", 0, 0, "upload"),
                    lambda: reader.prune_sessions(STARTED),
                    lambda: reader.put_folder_stats([]),
                ]
                for mutation in writes:
                    with pytest.raises(NimboError) as caught:
                        mutation()
                    assert caught.value.code == "db_readonly"

                resume.set()
                write.result(timeout=10)
                assert reader.get_remote("new") == new
                assert reader.get_meta(META_CHANGES_TOKEN) == "new"
        finally:
            resume.set()


def test_concurrent_writers_threads(db: Index) -> None:
    workers = 16
    per_worker = 200
    begin = Barrier(workers, timeout=10)

    def write(worker: int) -> None:
        begin.wait()
        for sequence in range(per_worker):
            db.upsert_remote([_file(f"{worker}-{sequence}", "root")])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(write, range(workers)))

    assert db.remote_count() == workers * per_worker
    assert set(db.load_tree("root").by_rel()) == {
        f"{worker}-{sequence}" for worker in range(workers) for sequence in range(per_worker)
    }
    assert db.integrity_ok()


def test_crash_between_statements_is_consistent(db_path: Path) -> None:
    existing = Op(OpKind.UPLOAD, "before-crash", "new")
    with closing(Index(db_path)) as index:
        previous_run = index.start_run("manual", STARTED)
        index.save_ops(previous_run, [existing])
        run_id = index.start_run("manual", STARTED)

    child = dedent("""
        import os
        import sys
        from pathlib import Path
        from nimbo.core.index import Index
        from nimbo.core.model import Op, OpKind

        path = Path(sys.argv[1])
        index = Index(path)
        run_id = int(sys.argv[2])
        index._connection.execute("PRAGMA cache_size = 3")
        index._connection.execute("PRAGMA cache_spill = ON")

        class CrashDuringIteration(list):
            def __iter__(self):
                for i, op in enumerate(super().__iter__()):
                    if i == 2:
                        count = index._connection.execute(
                            "SELECT count(*) FROM op WHERE run_id = ?", (run_id,)
                        ).fetchone()[0]
                        wal = path.with_name(path.name + "-wal")
                        if (
                            count == 2
                            and index._connection.in_transaction
                            and wal.stat().st_size > 32
                        ):
                            os._exit(1)
                        os._exit(2)
                    yield op

        index.save_ops(run_id, CrashDuringIteration([
            Op(OpKind.UPLOAD, f"item-{i}" + "x" * 131_072, "new") for i in range(5)
        ]))
        os._exit(3)
        """)
    process = subprocess.run(
        [sys.executable, "-c", child, str(db_path), str(run_id)],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert process.returncode == 1, process.stderr

    with closing(Index(db_path)) as index:
        assert index.op_states(previous_run) == [(existing, OpState.PENDING, None)]
        assert index.op_states(run_id) == []
        assert index.integrity_ok()


def test_integrity_ok(db: Index) -> None:
    assert db.integrity_ok()
    db._connection.execute("PRAGMA foreign_keys = OFF")
    db._connection.execute(
        "INSERT INTO op (run_id, seq, kind, rel, reason, state, updated_at) "
        "VALUES (1, 0, 'upload', 'item', 'new', 'pending', ?)",
        (STARTED,),
    )
    assert not db.integrity_ok()


def test_delete_subtree_never_reaches_target_root(tmp_path: Path) -> None:
    """Defense in depth: if the root was re-parented under a deleted folder, it must survive."""
    from nimbo.core.index import META_TARGET_ID, Index
    from nimbo.core.model import FOLDER_MIME, RemoteFile

    index = Index(tmp_path / "nimbo.db")
    index.set_meta(META_TARGET_ID, "root")
    index.upsert_remote(
        [
            RemoteFile(id="outside", name="Outside", mime_type=FOLDER_MIME, parents=("elsewhere",)),
            RemoteFile(id="root", name="FILES", mime_type=FOLDER_MIME, parents=("outside",)),
            RemoteFile(id="doc", name="a.txt", mime_type="text/plain", parents=("root",), size=1),
        ]
    )
    index.delete_remote(["outside"])
    assert index.get_remote("outside") is None
    assert index.get_remote("root") is not None
    assert index.get_remote("doc") is not None
    index.close()


def test_error_code_used_as_message_when_no_detail() -> None:
    from nimbo.core.errors import NimboError

    error = NimboError(code="custom_code")
    assert error.code == "custom_code"
    assert str(error) == "custom_code"
