"""Crash-safe SQLite mirror, cache, and operation journal."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import cast

from nimbo.core.errors import NimboError
from nimbo.core.model import FolderStat, Op, OpKind, OpState, RemoteFile, RunSummary, utc_now_iso
from nimbo.core.tree import RemoteTree

SCHEMA_VERSION = 1
META_ROOT_UUID = "root_uuid"
META_TARGET_ID = "target_root_id"
# Metadata key for the Drive changes cursor.
META_CHANGES_TOKEN = "changes_token"  # noqa: S105
META_LAST_FULL = "last_full_listing"
META_FORCE_FULL = "force_full_listing"
META_CHANGES_RELIABLE = "changes_reliable"
META_CHANGES_PROBE = "changes_probe"
META_QUOTA_USED = "quota_used"
META_QUOTA_LIMIT = "quota_limit"
META_ACCOUNT = "account_email"
META_LAST_DEEP = "last_deep_compare"

_MIGRATIONS = {
    1: (
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """
        CREATE TABLE remote (id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL,
          mime_type TEXT NOT NULL, size INTEGER, md5 TEXT, modified_ms INTEGER,
          properties TEXT NOT NULL DEFAULT '{}', trashed INTEGER NOT NULL DEFAULT 0,
          parents TEXT NOT NULL DEFAULT '[]')
        """,
        "CREATE INDEX remote_parent ON remote(parent_id)",
        """
        CREATE TABLE local_hash (rel TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER
          NOT NULL, md5 TEXT NOT NULL, hashed_at TEXT NOT NULL)
        """,
        """
        CREATE TABLE run (id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL,
          started_at TEXT NOT NULL, finished_at TEXT, result TEXT, summary TEXT)
        """,
        """
        CREATE TABLE op (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL
          REFERENCES run(id), seq INTEGER NOT NULL, kind TEXT NOT NULL, rel TEXT NOT NULL,
          reason TEXT NOT NULL, file_id TEXT, parent_rel TEXT, parent_id TEXT, src_rel TEXT,
          src_parent_id TEXT, name TEXT, size INTEGER NOT NULL DEFAULT 0,
          mtime_ns INTEGER NOT NULL DEFAULT 0, md5 TEXT, disk_rel TEXT,
          affected_files INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL)
        """,
        "CREATE INDEX op_state ON op(state)",
        """
        CREATE TABLE upload_session (rel TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER
          NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, url TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY (rel, size, mtime_ns, kind))
        """,
        """
        CREATE TABLE trash_log (run_id INTEGER NOT NULL, file_id TEXT NOT NULL, rel TEXT NOT NULL,
          is_folder INTEGER NOT NULL, PRIMARY KEY (run_id, file_id))
        """,
        """
        CREATE TABLE folder_stat (folder TEXT PRIMARY KEY, last_ok TEXT,
          files_local INTEGER NOT NULL DEFAULT 0, bytes_local INTEGER NOT NULL DEFAULT 0,
          files_pending INTEGER NOT NULL DEFAULT 0, bytes_pending INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT)
        """,
    ),
}

_UPSERT_REMOTE = """
    INSERT INTO remote (id, parent_id, name, mime_type, size, md5, modified_ms, properties,
                        trashed, parents)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, name=excluded.name,
      mime_type=excluded.mime_type, size=excluded.size, md5=excluded.md5,
      modified_ms=excluded.modified_ms, properties=excluded.properties,
      trashed=excluded.trashed, parents=excluded.parents
"""

_DELETE_REMOTE_SUBTREE = """
    DELETE FROM remote WHERE id IN (
        WITH RECURSIVE subtree(id) AS (
            SELECT ?
            UNION
            SELECT remote.id FROM remote JOIN subtree ON remote.parent_id = subtree.id
            -- Never recurse into the target root: deleting some other subtree must not be able
            -- to take the mirror's root (and everything below it) with it.
            WHERE remote.id NOT IN (SELECT value FROM meta WHERE key = 'target_root_id')
        )
        SELECT id FROM subtree
    )
"""

_INSERT_OP = """
    INSERT INTO op (run_id, seq, kind, rel, reason, file_id, parent_rel, parent_id, src_rel,
                    src_parent_id, name, size, mtime_ns, md5, disk_rel, affected_files, state,
                    updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
"""

_Parameters = tuple[str | int | None, ...]


def _checked(parameters: _Parameters) -> _Parameters:
    # SQLite TEXT is UTF-8. Validate even JSON payloads before binding them.
    for value in parameters:
        if isinstance(value, str):
            value.encode("utf-8")
    return parameters


def _remote_row(row: sqlite3.Row) -> RemoteFile:
    return RemoteFile(
        id=row["id"],
        name=row["name"],
        mime_type=row["mime_type"],
        parents=tuple(json.loads(row["parents"])),
        size=row["size"],
        md5=row["md5"],
        modified_ms=row["modified_ms"],
        properties=json.loads(row["properties"]),
        trashed=bool(row["trashed"]),
    )


def _op_row(row: sqlite3.Row) -> Op:
    return Op(
        kind=OpKind(row["kind"]),
        rel=row["rel"],
        reason=row["reason"],
        file_id=row["file_id"],
        parent_rel=row["parent_rel"],
        parent_id=row["parent_id"],
        src_rel=row["src_rel"],
        src_parent_id=row["src_parent_id"],
        name=row["name"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        md5=row["md5"],
        affected_files=row["affected_files"],
        disk_rel=row["disk_rel"],
        db_id=row["id"],
    )


class Index:
    """Serve transactional access to one SQLite index connection across threads.

    Mutations on a read-only index raise ``NimboError`` with code ``db_readonly``.
    Strings containing Unicode surrogates raise ``ValueError`` before binding.
    Updates to individual journal operations require a persisted ``Op.db_id`` and
    raise ``ValueError`` if the ID is missing or unknown.
    """

    def __init__(self, path: Path) -> None:
        """Create or migrate a private WAL database, or raise ``db_too_new``."""
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._open(path, readonly=False)

    @classmethod
    def open_readonly(cls, path: Path) -> Index:
        """Open an existing database without migrating it, or raise ``db_too_new``."""
        index = cls.__new__(cls)
        index._open(path, readonly=True)
        return index

    def _open(self, path: Path, *, readonly: bool) -> None:
        self._lock = RLock()
        self._readonly = readonly
        mode = "ro" if readonly else "rw"
        self._connection = sqlite3.connect(
            f"{path.absolute().as_uri()}?mode={mode}",
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.row_factory = sqlite3.Row
            self._schema_version()
            if not readonly:
                journal = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if journal[0] != "wal":
                    raise sqlite3.OperationalError("Could not enable SQLite WAL mode")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if not readonly:
                self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def _schema_version(self) -> int:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise NimboError(code="db_too_new")
        return version

    def _migrate(self) -> None:
        with self._transaction(write=True):
            for version in range(self._schema_version() + 1, SCHEMA_VERSION + 1):
                # executescript would commit the transaction before running the DDL.
                for statement in _MIGRATIONS[version]:
                    self._connection.execute(statement)
                self._connection.execute(f"PRAGMA user_version = {version}")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[None]:
        with self._lock:
            if write and self._readonly:
                raise NimboError(code="db_readonly")
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self._connection.commit()
            finally:
                if self._connection.in_transaction:
                    self._connection.rollback()

    def _execute(self, sql: str, parameters: _Parameters = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, _checked(parameters))

    def _executemany(self, sql: str, rows: Iterable[_Parameters]) -> sqlite3.Cursor:
        return self._connection.executemany(sql, map(_checked, rows))

    def close(self) -> None:
        """Close the connection after any transaction in another thread finishes."""
        with self._lock:
            self._connection.close()

    def get_meta(self, key: str) -> str | None:
        """Return the exact value for ``key``, or ``None`` if absent."""
        with self._transaction():
            row = self._execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return None if row is None else cast(str, row["value"])

    def set_meta(self, key: str, value: str | None) -> None:
        """Store ``value`` for ``key``, deleting the entry if ``value`` is ``None``."""
        with self._transaction(write=True):
            self._set_meta(key, value)

    def _set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def replace_remote(
        self, files: Iterable[RemoteFile], *, meta: Mapping[str, str | None] = {}
    ) -> None:
        """Replace the entire remote mirror and apply metadata changes atomically."""
        with self._transaction(write=True):
            self._execute("DELETE FROM remote")
            self._upsert_remote(files)
            for key, value in meta.items():
                self._set_meta(key, value)

    def apply_remote_delta(
        self,
        upserts: Iterable[RemoteFile],
        delete_subtrees: Iterable[str],
        *,
        meta: Mapping[str, str | None] = {},
    ) -> None:
        """Apply upserts, subtree removals, and metadata changes in one transaction."""
        with self._transaction(write=True):
            # Moves detach surviving descendants before their old parents are removed.
            self._upsert_remote(upserts)
            self._delete_remote(delete_subtrees)
            for key, value in meta.items():
                self._set_meta(key, value)

    def upsert_remote(self, files: Iterable[RemoteFile]) -> None:
        """Insert or replace mirror rows by Drive ID in one transaction."""
        with self._transaction(write=True):
            self._upsert_remote(files)

    def _upsert_remote(self, files: Iterable[RemoteFile]) -> None:
        self._executemany(
            _UPSERT_REMOTE,
            (
                (
                    item.id,
                    item.parents[0] if item.parents else None,
                    item.name,
                    item.mime_type,
                    item.size,
                    item.md5,
                    item.modified_ms,
                    json.dumps(dict(item.properties), ensure_ascii=False),
                    item.trashed,
                    json.dumps(item.parents, ensure_ascii=False),
                )
                for item in files
            ),
        )

    def delete_remote(self, file_ids: Iterable[str]) -> None:
        """Remove the supplied IDs and all descendants linked by their first parent."""
        with self._transaction(write=True):
            self._delete_remote(file_ids)

    def _delete_remote(self, file_ids: Iterable[str]) -> None:
        self._executemany(_DELETE_REMOTE_SUBTREE, ((file_id,) for file_id in file_ids))

    def get_remote(self, file_id: str) -> RemoteFile | None:
        """Return the stored Drive item, including trashed rows, or ``None``."""
        with self._transaction():
            row = self._execute("SELECT * FROM remote WHERE id = ?", (file_id,)).fetchone()
            return None if row is None else _remote_row(row)

    def in_subtree(self, file_id: str, root_id: str) -> bool:
        """Return whether a live row reaches the given root through indexed parents.

        The root identifies itself. Missing rows or cycles before reaching it
        do not establish membership.
        """
        if file_id == root_id:
            return True
        with self._transaction():
            row = self._execute(
                """
                WITH RECURSIVE ancestors(id, parent_id) AS (
                    SELECT id, parent_id FROM remote WHERE id = ? AND trashed = 0
                    UNION
                    SELECT parent.id, parent.parent_id
                    FROM remote AS parent JOIN ancestors AS child ON parent.id = child.parent_id
                    WHERE child.id <> ? AND parent.trashed = 0
                )
                SELECT EXISTS(SELECT 1 FROM ancestors WHERE id = ?)
                """,
                (file_id, root_id, root_id),
            ).fetchone()
            return bool(row[0])

    def load_tree(self, root_id: str) -> RemoteTree:
        """Build a tree rooted at ``root_id`` from a consistent mirror snapshot."""
        with self._transaction():
            _checked((root_id,))
            return RemoteTree(root_id, map(_remote_row, self._execute("SELECT * FROM remote")))

    def remote_count(self) -> int:
        """Return the total number of stored remote rows."""
        with self._transaction():
            return int(self._execute("SELECT count(*) FROM remote").fetchone()[0])

    def get_hash(self, rel: str, size: int, mtime_ns: int) -> str | None:
        """Return a cached hash only if its path, size, and mtime all match."""
        with self._transaction():
            row = self._execute(
                "SELECT md5 FROM local_hash WHERE rel = ? AND size = ? AND mtime_ns = ?",
                (rel, size, mtime_ns),
            ).fetchone()
            return None if row is None else cast(str, row["md5"])

    def put_hash(self, rel: str, size: int, mtime_ns: int, md5: str) -> None:
        """Store the latest cached hash and file metadata for ``rel``."""
        with self._transaction(write=True):
            self._put_hash(rel, size, mtime_ns, md5)

    def _put_hash(self, rel: str, size: int, mtime_ns: int, md5: str) -> None:
        self._execute(
            "INSERT INTO local_hash (rel, size, mtime_ns, md5, hashed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(rel) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "md5=excluded.md5, hashed_at=excluded.hashed_at",
            (rel, size, mtime_ns, md5, utc_now_iso()),
        )

    def prune_hashes(self, keep: Collection[str]) -> int:
        """Remove hashes for paths outside ``keep`` and return the number removed."""
        with self._transaction(write=True):
            retained = set(keep)
            for rel in retained:
                _checked((rel,))
            to_remove = [
                (row[0],)
                for row in self._execute("SELECT rel FROM local_hash")
                if row[0] not in retained
            ]
            return self._executemany("DELETE FROM local_hash WHERE rel = ?", to_remove).rowcount

    def start_run(self, trigger: str, started_at: str) -> int:
        """Record the start of a run and return its newly allocated ID."""
        with self._transaction(write=True):
            cursor = self._execute(
                "INSERT INTO run (trigger, started_at) VALUES (?, ?)", (trigger, started_at)
            )
            run_id = cursor.lastrowid
            assert run_id is not None
            return run_id

    def finish_run(self, summary: RunSummary) -> None:
        """Store the complete summary, raising ``ValueError`` without an existing run ID."""
        with self._transaction(write=True):
            cursor = self._execute(
                "UPDATE run SET finished_at = ?, result = ?, summary = ? WHERE id = ?",
                (
                    summary.finished_at,
                    summary.result,
                    json.dumps(summary.to_dict(), ensure_ascii=False),
                    summary.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("A persisted run ID is required")

    def get_run(self, run_id: int) -> RunSummary | None:
        """Return the stored summary, or ``None`` for an unfinished or absent run."""
        with self._transaction():
            row = self._execute("SELECT summary FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["summary"] is None:
                return None
            return RunSummary.from_dict(json.loads(row["summary"]))

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        """Return finished runs by descending run ID, with no cap if ``limit`` is negative."""
        with self._transaction():
            rows = self._execute(
                "SELECT summary FROM run WHERE summary IS NOT NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [RunSummary.from_dict(json.loads(row["summary"])) for row in rows]

    def last_run(self) -> RunSummary | None:
        """Return the newest finished run's summary, or ``None`` if there is none."""
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    def save_ops(self, run_id: int, ops: Sequence[Op]) -> None:
        """Append pending operations to a run and assign IDs after commit."""
        assigned: list[tuple[Op, int]] = []
        with self._lock:
            with self._transaction(write=True):
                updated_at = utc_now_iso()
                first = self._execute(
                    "SELECT coalesce(max(seq) + 1, 0) FROM op WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
                for seq, op in enumerate(ops, start=first):
                    cursor = self._execute(
                        _INSERT_OP,
                        (
                            run_id,
                            seq,
                            op.kind,
                            op.rel,
                            op.reason,
                            op.file_id,
                            op.parent_rel,
                            op.parent_id,
                            op.src_rel,
                            op.src_parent_id,
                            op.name,
                            op.size,
                            op.mtime_ns,
                            op.md5,
                            op.disk_rel,
                            op.affected_files,
                            updated_at,
                        ),
                    )
                    op_id = cursor.lastrowid
                    assert op_id is not None
                    assigned.append((op, op_id))
            for op, op_id in assigned:
                op.db_id = op_id

    def _update_op(
        self, op: Op, state: OpState, *, error: str | None = None, increment_attempts: bool = False
    ) -> None:
        cursor = self._execute(
            "UPDATE op SET state = ?, error = ?, attempts = attempts + ?, updated_at = ? "
            "WHERE id = ?",
            (state, error, increment_attempts, utc_now_iso(), op.db_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("A persisted operation ID is required")

    def mark_inflight(self, op: Op) -> None:
        """Mark a persisted operation in flight, clear its error, and count the attempt."""
        with self._transaction(write=True):
            self._update_op(op, OpState.INFLIGHT, increment_attempts=True)

    def complete_op(
        self,
        op: Op,
        *,
        upsert: Sequence[RemoteFile] = (),
        remove_subtrees: Sequence[str] = (),
        trash_log_run: int | None = None,
        trashed_is_folder: bool = False,
        local_md5: str | None = None,
    ) -> None:
        """Mark an operation done with its mirror, trash log and verified digest changes.

        A trash log entry requires ``op.file_id``, otherwise ``ValueError`` is raised.
        """
        with self._transaction(write=True):
            self._update_op(op, OpState.DONE)
            self._upsert_remote(upsert)
            self._delete_remote(remove_subtrees)
            if local_md5 is not None:
                self._put_hash(op.rel, op.size, op.mtime_ns, local_md5)
            if trash_log_run is not None:
                if op.file_id is None:
                    raise ValueError("A Drive file ID is required for the trash log")
                self._execute(
                    "INSERT INTO trash_log (run_id, file_id, rel, is_folder) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id, file_id) DO UPDATE SET rel=excluded.rel, "
                    "is_folder=excluded.is_folder",
                    (trash_log_run, op.file_id, op.rel, trashed_is_folder),
                )

    def fail_op(self, op: Op, error_code: str) -> None:
        """Mark a persisted operation failed with the given machine-readable error code."""
        with self._transaction(write=True):
            self._update_op(op, OpState.FAILED, error=error_code)

    def reset_op(self, op: Op) -> None:
        """Return a persisted operation to pending and clear the previous error."""
        with self._transaction(write=True):
            self._update_op(op, OpState.PENDING)

    def inflight_ops(self) -> list[Op]:
        """Return unconfirmed writes, including legacy failed trashes with uncertain outcomes."""
        with self._transaction():
            rows = self._execute(
                """
                SELECT * FROM op WHERE state = ?
                OR (kind = ? AND state = ? AND error IN ('network_failed', 'server_error'))
                ORDER BY id
                """,
                (OpState.INFLIGHT, OpKind.TRASH, OpState.FAILED),
            )
            return [_op_row(row) for row in rows]

    def abandon_ops(self, db_ids: Sequence[int]) -> None:
        """Mark the specified journal rows abandoned in one transaction."""
        with self._transaction(write=True):
            updated_at = utc_now_iso()
            self._executemany(
                "UPDATE op SET state = ?, updated_at = ? WHERE id = ?",
                ((OpState.ABANDONED, updated_at, op_id) for op_id in db_ids),
            )

    def op_states(self, run_id: int) -> list[tuple[Op, OpState, str | None]]:
        """Return a run's operations, states, and errors in sequence order."""
        with self._transaction():
            rows = self._execute("SELECT * FROM op WHERE run_id = ? ORDER BY seq, id", (run_id,))
            return [(_op_row(row), OpState(row["state"]), row["error"]) for row in rows]

    def trash_log(self, run_id: int) -> list[tuple[str, str, bool]]:
        """Return the logged file ID, rel, and folder flag for each entry of a run."""
        with self._transaction():
            rows = self._execute(
                "SELECT file_id, rel, is_folder FROM trash_log WHERE run_id = ? ORDER BY file_id",
                (run_id,),
            )
            return [(row["file_id"], row["rel"], bool(row["is_folder"])) for row in rows]

    def last_run_with_trash(self) -> int | None:
        """Return the newest run ID with a trash log, including unfinished runs."""
        with self._transaction():
            return cast(
                int | None, self._execute("SELECT max(run_id) FROM trash_log").fetchone()[0]
            )

    def get_session(self, rel: str, size: int, mtime_ns: int, kind: str) -> tuple[str, str] | None:
        """Return the saved file ID and URL for an exact upload cache key."""
        with self._transaction():
            row = self._execute(
                "SELECT file_id, url FROM upload_session "
                "WHERE rel = ? AND size = ? AND mtime_ns = ? AND kind = ?",
                (rel, size, mtime_ns, kind),
            ).fetchone()
            return None if row is None else (row["file_id"], row["url"])

    def put_session(
        self, rel: str, size: int, mtime_ns: int, kind: str, file_id: str, url: str
    ) -> None:
        """Persist the file ID and URL for an upload session, refreshing its timestamp."""
        with self._transaction(write=True):
            self._execute(
                "INSERT INTO upload_session (rel, size, mtime_ns, kind, file_id, url, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(rel, size, mtime_ns, kind) DO UPDATE SET "
                "file_id=excluded.file_id, url=excluded.url, created_at=excluded.created_at",
                (rel, size, mtime_ns, kind, file_id, url, utc_now_iso()),
            )

    def delete_session(self, rel: str, size: int, mtime_ns: int, kind: str) -> None:
        """Delete the saved upload session matching the full cache key."""
        with self._transaction(write=True):
            self._execute(
                "DELETE FROM upload_session "
                "WHERE rel = ? AND size = ? AND mtime_ns = ? AND kind = ?",
                (rel, size, mtime_ns, kind),
            )

    def prune_sessions(self, older_than: str) -> int:
        """Delete sessions older than the ISO 8601 cutoff and return the count.

        A cutoff without a timezone is interpreted as UTC.
        """
        with self._transaction(write=True):
            _checked((older_than,))
            cutoff = datetime.fromisoformat(older_than)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)
            return self._execute(
                "DELETE FROM upload_session WHERE created_at < ?",
                (cutoff.astimezone(UTC).isoformat(),),
            ).rowcount

    def put_folder_stats(self, stats: Iterable[FolderStat]) -> None:
        """Insert or update the supplied folder stats while preserving all their fields."""
        with self._transaction(write=True):
            self._executemany(
                "INSERT INTO folder_stat (folder, last_ok, files_local, bytes_local, "
                "files_pending, bytes_pending, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(folder) DO UPDATE SET last_ok=excluded.last_ok, "
                "files_local=excluded.files_local, bytes_local=excluded.bytes_local, "
                "files_pending=excluded.files_pending, bytes_pending=excluded.bytes_pending, "
                "updated_at=excluded.updated_at",
                (
                    (
                        stat.folder,
                        stat.last_ok,
                        stat.files_local,
                        stat.bytes_local,
                        stat.files_pending,
                        stat.bytes_pending,
                        stat.updated_at,
                    )
                    for stat in stats
                ),
            )

    def folder_stats(self) -> dict[str, FolderStat]:
        """Return the persisted folder stats keyed by folder name."""
        with self._transaction():
            return {
                row["folder"]: FolderStat(
                    folder=row["folder"],
                    last_ok=row["last_ok"],
                    files_local=row["files_local"],
                    bytes_local=row["bytes_local"],
                    files_pending=row["files_pending"],
                    bytes_pending=row["bytes_pending"],
                    updated_at=row["updated_at"],
                )
                for row in self._execute("SELECT * FROM folder_stat ORDER BY folder")
            }

    def integrity_ok(self) -> bool:
        """Return whether SQLite's structural and foreign-key integrity checks pass."""
        with self._transaction():
            return [row[0] for row in self._execute("PRAGMA integrity_check")] == [
                "ok"
            ] and self._execute("PRAGMA foreign_key_check").fetchone() is None
