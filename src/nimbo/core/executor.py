"""Apply a persisted mirror plan, with a durable intent before each Drive write."""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from threading import Event, Lock

from .drive import LIST_GROUP_MAX, DriveClient, ForeignItemKept, ForeignTarget, _literal
from .errors import (
    AuthError,
    BadRequest,
    DriveError,
    FileChanged,
    Forbidden,
    Md5Mismatch,
    NimboError,
    NotFound,
    RateLimited,
    RemoteChanged,
    ServerError,
    Stopped,
    StorageQuotaExceeded,
    TransportFailed,
    Unauthorized,
    UploadLimitExceeded,
)
from .index import META_CHANGES_RELIABLE, META_CHANGES_TOKEN, META_FORCE_FULL, Index, TrashFile
from .model import (
    FILE_FIELDS,
    FOREIGN_ITEM_KEPT,
    OWNER_PROPERTY,
    FileVersion,
    Op,
    OpKind,
    Phase,
    PlanIssue,
    RemoteFile,
    drive_ms,
    rel_join,
)
from .progress import ProgressTracker
from .transfer import Uploader, UploadRequest
from .tree import RemoteTree, _rel_component

_CREATE = {OpKind.MKDIR, OpKind.UPLOAD}
_WITH_PARENT = _CREATE | {OpKind.MOVE}
_UPLOAD = {OpKind.UPLOAD, OpKind.UPDATE}
TRASH_TOOK_NEW_ITEMS = "trash-took-new-items"
_CHANGE_POLL_INTERVAL = 1.0
_PROGRESS_PHASE = {
    Phase.MKDIR: "mkdir",
    Phase.MOVE: "move",
    Phase.CONTENT: "transfer",
    Phase.TRASH: "trash",
}


@dataclass(slots=True)
class ExecReport:
    """Summarize committed operations, successful upload bytes, and journaled failures."""

    done: Counter[OpKind]
    uploaded_bytes: int
    failed: list[tuple[str, str]]
    stop_reason: str | None
    created_ids: list[str]
    warnings: list[PlanIssue] = field(default_factory=list)


class _CreateFolderCache:
    def __init__(
        self,
        drive: DriveClient,
        index: Index,
        root: str,
        root_uuid: str,
        clock: Callable[[], float],
        refresh_requested: Event,
    ) -> None:
        self._drive = drive
        self._index = index
        self._root = root
        self._root_uuid = root_uuid
        self._clock = clock
        self._refresh_requested = refresh_requested
        self._verified: dict[str, tuple[str, float]] = {}
        self._cursor: str | None = None
        self._next_poll = clock() + _CHANGE_POLL_INTERVAL
        self._first_poll_due = False
        self._has_polled = False
        self._blocked = False
        self._lock = Lock()

    def checkpoint(self) -> None:
        """Read the durable cursor after the scheduler refreshes the index."""
        cursor = self._index.get_meta(META_CHANGES_TOKEN)
        if self._index.get_meta(META_CHANGES_RELIABLE) == "0":
            cursor = None
        with self._lock:
            if (cursor is None) != (self._cursor is None):
                self._verified.clear()
            self._cursor = cursor
            self._blocked = False

    def file_created(self) -> None:
        """Check the feed on the next create after the first confirmed file write."""
        with self._lock:
            self._first_poll_due = not self._has_polled

    def remember_directory(self, item: RemoteFile) -> None:
        """Extend the verified path using Drive's confirmed mkdir response."""
        if not item.is_folder or item.trashed or len(item.parents) != 1:
            return
        with self._lock:
            parent = self._verified.get(item.parents[0])
            if parent is not None:
                rel, expires = parent
                self._verified[item.id] = (rel_join(rel, _rel_component(item.name)), expires)

    def check(self, folder: str, rel: str | None) -> bool | None:
        """Check the creation parent, or require live checks during feed recovery."""
        # Serialize the poll and cache misses so active uploads share their results.
        with self._lock:
            now = self._clock()
            self._poll(now)
            if self._blocked:
                return None
            actual = self._verify_path(folder, now)
            return actual is not None and (rel is None or actual == rel)

    def _poll(self, now: float) -> None:
        if self._cursor is None or self._blocked:
            return
        if now < self._next_poll and not self._first_poll_due:
            return
        self._has_polled = True
        self._first_poll_due = False
        try:
            page = self._drive.changes(self._cursor, single_attempt=True)
        except DriveError:
            self._require_refresh()
            return
        finally:
            self._next_poll = self._clock() + _CHANGE_POLL_INTERVAL
        if page.next_page_token or not page.new_start_page_token:
            self._require_refresh()
            return
        self._cursor = page.new_start_page_token
        latest: dict[str, RemoteFile | None] = {}
        for change in page.changes:
            if not change.removed and (change.file is None or change.file.id != change.file_id):
                self._require_refresh()
                return
            latest[change.file_id] = None if change.removed else change.file
        for file_id, live in latest.items():
            if self._folder_changed(file_id, live):
                self._require_refresh()
                return

    def _require_refresh(self) -> None:
        self._verified.clear()
        self._blocked = True
        self._refresh_requested.set()

    def _folder_changed(self, file_id: str, live: RemoteFile | None) -> bool:
        if live is not None and live.is_folder:
            known = self._index.get_remote(file_id)
            if known is not None:
                # A mkdir or move by this run is already recorded in the index.
                return (
                    live.name,
                    live.parents,
                    live.trashed,
                    live.mime_type,
                    live.owned_by_me,
                    live.owner_root,
                ) != (
                    known.name,
                    known.parents,
                    known.trashed,
                    known.mime_type,
                    known.owned_by_me,
                    known.owner_root,
                )
            return (
                file_id in self._verified
                or file_id == self._root
                or any(self._index.in_subtree(parent, self._root) for parent in live.parents)
            )
        if file_id in self._verified or file_id == self._root:
            return True
        before = self._index.get_remote(file_id)
        return before is not None and before.is_folder

    def _verify_path(self, folder: str, now: float) -> str | None:
        seen: set[str] = set()
        trail: list[RemoteFile] = []
        while folder not in seen:
            seen.add(folder)
            proof = self._verified.get(folder)
            if proof is not None and (self._cursor is not None or now < proof[1]):
                rel, expires = proof
                break
            item = self._drive.get(folder)
            if item is None or item.id != folder or item.trashed or not item.is_folder:
                return None
            if folder == self._root:
                if item.owner_root not in (None, self._root_uuid):
                    raise ForeignTarget
                rel, expires = "", now + _CHANGE_POLL_INTERVAL
                self._verified[folder] = (rel, expires)
                break
            if item.protected_from(self._root_uuid):
                raise ForeignItemKept
            if len(item.parents) != 1:
                return None
            trail.append(item)
            folder = item.parents[0]
        else:
            return None
        for item in reversed(trail):
            rel = rel_join(rel, _rel_component(item.name))
            self._verified[item.id] = (rel, expires)
        return rel

    def clear(self) -> None:
        with self._lock:
            self._verified.clear()


@dataclass(slots=True)
class _Run:
    id: int
    stop: Event
    planned: RemoteTree
    moving: set[str]
    unconfirmed_trash: set[int]
    create_folder_cache: _CreateFolderCache | None = None
    report: ExecReport = field(default_factory=lambda: ExecReport(Counter(), 0, [], None, []))
    lock: Lock = field(default_factory=Lock)
    aborted: Event = field(default_factory=Event)
    created_folders: set[str] = field(default_factory=set)
    pending_probe: list[str] = field(default_factory=list)
    last_refresh: float | None = None
    ops_since_refresh: int = 0
    refresh_requested: Event = field(default_factory=Event)
    refreshed: RemoteTree | None = None
    names_by_parent: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    trash_followups: deque[_Work] = field(default_factory=deque)
    drift_rel: str | None = None
    index_dirty: bool = False

    def request_stop(self, reason: str) -> None:
        with self.lock:
            if self.report.stop_reason is None:
                self.report.stop_reason = reason

    def can_start(self) -> bool:
        with self.lock:
            return self._can_start()

    def begin(self, index: Index, op: Op) -> bool:
        with self.lock:
            if not self._can_start():
                return False
            index.mark_inflight(op)
            return True

    def _can_start(self) -> bool:
        if self.report.stop_reason is None and self.stop.is_set():
            self.report.stop_reason = "cancelled"
        return self.report.stop_reason is None and not self.aborted.is_set()

    def matching_children(self, parent: str, name: str) -> Sequence[str]:
        assert self.refreshed is not None
        with self.lock:
            names = self.names_by_parent.get(parent)
            if names is None:
                names = {}
                for item in self.refreshed.children(parent):
                    names.setdefault(_rel_component(item.name), []).append(item.id)
                self.names_by_parent[parent] = names
            return names.get(name, ())


@dataclass(slots=True)
class _Work:
    op: Op
    deferrals: int = 0
    md5_retries: int = 0
    trash_uncertain: bool = False


class _Retry(Enum):
    IMMEDIATE = auto()
    DEFERRED = auto()


class _LocalReadError(NimboError):
    pass


class _ProgressError(RuntimeError):
    pass


class _TrashConfirmed(Exception):
    def __init__(self, item: RemoteFile) -> None:
        self.item = item


class _FolderKept(RemoteChanged):
    def __init__(self, children: list[Op], warnings: list[PlanIssue]) -> None:
        super().__init__()
        self.children = children
        self.warnings = warnings


def _same_item(planned: RemoteFile, current: RemoteFile | None) -> bool:
    if current is None or current.trashed or current.is_google_native:
        return False
    if planned.is_folder and current.is_folder:
        # Folder timestamps can change as a side effect of edits to their children.
        return (
            planned.id,
            planned.name,
            planned.parents,
            planned.properties,
            planned.owned_by_me,
        ) == (
            current.id,
            current.name,
            current.parents,
            current.properties,
            current.owned_by_me,
        )
    return current == planned


def _planned_type_conflict(tree: RemoteTree, op: Op, item: RemoteFile) -> bool:
    # The replacement is created before any trash. A new collision requires replanning.
    if op.kind not in _CREATE or item.is_folder == (op.kind is OpKind.MKDIR):
        return False
    planned = tree.get(item.id)
    return planned is not None and _same_item(planned, item)


def _inherited_trash_children(
    drive: DriveClient, parents: Sequence[str], should_stop: Callable[[], bool]
) -> Iterator[RemoteFile]:
    # An explicitly trashed child (and its subtree) was already in the trash.
    # The ordinary child listing discards this field when parsing RemoteFile.
    clauses = " or ".join(f"{_literal(parent)} in parents" for parent in parents)
    params: dict[str, str | int] = {
        "fields": f"nextPageToken,files({FILE_FIELDS},explicitlyTrashed)",
        "q": f"({clauses}) and trashed = true",
        "spaces": "drive",
        "pageSize": 1000,
    }
    for raw in drive._file_pages(params, should_stop=should_stop):
        if raw.get("trashed") and not raw.get("explicitlyTrashed"):
            yield RemoteFile.from_api(raw)


def _audit_folder_trash(
    drive: DriveClient, index: Index, op: Op, should_stop: Callable[[], bool]
) -> list[TrashFile]:
    guarded = index.guarded_trash_ids(op)
    if guarded is None:
        return []  # Older intents have no reliable baseline.
    assert op.file_id is not None
    pending = deque([(op.file_id, op.rel)])
    seen = {op.file_id}
    unexpected = []
    while pending:
        group = dict(pending.popleft() for _ in range(min(len(pending), LIST_GROUP_MAX)))
        for child in _inherited_trash_children(drive, tuple(group), should_stop):
            if len(child.parents) != 1 or child.parents[0] not in group or child.id in seen:
                continue
            seen.add(child.id)
            rel = rel_join(group[child.parents[0]], _rel_component(child.name))
            if child.id not in guarded:
                unexpected.append(
                    TrashFile(child.id, rel, child.size, child.md5, is_folder=child.is_folder)
                )
            if child.is_folder:
                pending.append((child.id, rel))
    return sorted(unexpected, key=lambda item: (item.rel, item.file_id))


def _first_drift(before: RemoteTree, after: RemoteTree) -> str | None:
    for rel, item in before.walk():
        current = after.get(item.id)
        if item.id not in after or (current != item and not _same_item(item, current)):
            return rel
    return next((rel for rel, item in after.walk() if item.id not in before), None)


def _stop_reason(error: NimboError) -> str | None:
    if isinstance(error, ForeignTarget):
        return error.code
    if isinstance(error, StorageQuotaExceeded):
        return "storage_quota_exceeded"
    if isinstance(error, UploadLimitExceeded):
        return "upload_limit_exceeded"
    if isinstance(error, AuthError):
        return "reauth_required" if error.needs_reconnect else "auth_failed"
    if isinstance(error, Unauthorized):
        return "auth_failed"
    if isinstance(error, (TransportFailed, ServerError, RateLimited)):
        return "network_failed"
    return None


class Executor:
    """Execute journaled Drive operations with bounded content concurrency."""

    def __init__(
        self,
        *,
        drive: DriveClient,
        uploader: Uploader,
        index: Index,
        source_root: Path,
        root_uuid: str,
        tracker: ProgressTracker,
        stop: Event,
        parallel: int = 4,
        changed_retries: int = 3,
        md5_retries: int = 2,
        root_id: str | None = None,
        refresh_remote: Callable[[], object] | None = None,
        refresh_every: float = 60.0,
        refresh_ops: int = 500,
        clock: Callable[[], float] = time.monotonic,
        record_probe: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        """Bind the run's dependencies and set retry budgets.

        Guard writes under `root_id` when supplied. Refresh the mirror on the
        scheduling thread before phases and after the time or operation limit. Send
        newly created IDs to `record_probe` before consuming their feed window.
        Raise ValueError for invalid retry budgets, parallelism or refresh limits.
        """
        if parallel < 1 or changed_retries < 1 or md5_retries < 0:
            raise ValueError("Expected positive parallel and changed_retries, and md5_retries >= 0")
        if not math.isfinite(refresh_every) or refresh_every < 0:
            raise ValueError("refresh_every must be finite and nonnegative")
        if refresh_ops < 1:
            raise ValueError("refresh_ops must be positive")
        self._drive = drive
        self._uploader = uploader
        self._index = index
        self._source_root = source_root
        self._real_source_root = source_root.resolve()
        self._properties = {OWNER_PROPERTY: root_uuid}
        self._root_uuid = root_uuid
        self._tracker = tracker
        self._stop = stop
        self._parallel = parallel
        self._changed_retries = changed_retries
        self._md5_retries = md5_retries
        self._root_id = root_id
        self._refresh_remote = refresh_remote
        self._refresh_every = refresh_every
        self._refresh_ops = refresh_ops
        self._clock = clock
        self._record_probe = record_probe

    def execute(self, run_id: int, ops: Sequence[Op]) -> ExecReport:
        """Apply the supplied persisted operations and return their execution report.

        Raise ValueError for missing journal IDs, Drive IDs, or required parents
        and create names before touching Drive. Unexpected errors propagate and
        leave any unconfirmed remote writes in flight for reconciliation.
        """
        phases: dict[Phase, deque[_Work]] = {phase: deque() for phase in Phase}
        for op in ops:
            if op.db_id is None:
                raise ValueError(f"{op.kind} {op.rel!r} requires a persisted db_id")
            if not op.file_id:
                raise ValueError(f"{op.kind} {op.rel!r} requires a file_id")
            if op.kind in _WITH_PARENT and not op.parent_id:
                raise ValueError(f"{op.kind} {op.rel!r} requires a parent_id")
            if op.kind in _CREATE and op.name is None:
                raise ValueError(f"{op.kind} {op.rel!r} requires a name")
            phases[op.phase].append(_Work(op))

        root = self._root_id
        run = _Run(
            run_id,
            self._stop,
            self._index.load_tree(root or ""),
            {op.file_id for op in ops if op.kind is OpKind.MOVE and op.file_id is not None},
            {
                op.db_id
                for op in self._index.inflight_ops()
                if op.kind is OpKind.TRASH and op.db_id is not None
            },
        )
        if root is not None and self._refresh_remote is not None:
            run.create_folder_cache = _CreateFolderCache(
                self._drive, self._index, root, self._root_uuid, self._clock, run.refresh_requested
            )
        for phase, pending in phases.items():
            if not run.can_start() or not self._refresh(run):
                break
            if phase is Phase.CONTENT:
                self._tracker.phase(
                    _PROGRESS_PHASE[phase],
                    ops_total=len(pending),
                    bytes_total=sum(work.op.size for work in pending if work.op.kind in _UPLOAD),
                )
                self._content(run, pending)
            else:
                self._tracker.phase(_PROGRESS_PHASE[phase], ops_total=len(pending))
                while pending and run.can_start():
                    if self._refresh_due(run) and not self._refresh(run):
                        break
                    work = pending.popleft()
                    run.ops_since_refresh += 1
                    if self._execute_one(run, work):
                        pending.append(work)
                    pending.extendleft(reversed(run.trash_followups))
                    run.trash_followups.clear()

        # The last phase can commit writes after its initial refresh.
        if run.can_start() and run.index_dirty and self._root_id is not None:
            self._refresh(run)
        if run.drift_rel is not None and not run.report.failed:
            run.report.failed.append((run.drift_rel, "remote_changed"))
        self._tracker.flush(force=True)
        self._probe_created(run)
        return run.report

    def _refresh_due(self, run: _Run) -> bool:
        return (
            self._refresh_remote is not None
            and run.last_refresh is not None
            and (
                run.refresh_requested.is_set()
                or run.ops_since_refresh >= self._refresh_ops
                or self._clock() - run.last_refresh >= self._refresh_every
            )
        )

    def _refresh(self, run: _Run) -> bool:
        if self._refresh_remote is None:
            return True
        root = self._root_id
        before = run.refreshed if run.refreshed is not None else run.planned
        if root is not None and run.index_dirty:
            before = self._index.load_tree(root)
        force_full = self._index.get_meta(META_FORCE_FULL)
        try:
            self._probe_created(run)
            self._refresh_remote()
        except NimboError as exc:
            reason: str | None
            if isinstance(exc, Stopped):
                reason = "cancelled"
            elif exc.code == "target_missing":
                reason = exc.code
            else:
                reason = _stop_reason(exc)
            if reason is None:
                raise
            run.request_stop(reason)
            return False
        finally:
            # A listing fallback must not clear a failed op's request for the
            # next plan to start from a full snapshot.
            if force_full is not None and self._index.get_meta(META_FORCE_FULL) is None:
                self._index.set_meta(META_FORCE_FULL, force_full)
        run.last_refresh = self._clock()
        run.ops_since_refresh = 0
        run.refresh_requested.clear()
        if run.create_folder_cache is not None:
            run.create_folder_cache.checkpoint()
        if root is not None:
            run.refreshed = self._index.load_tree(root)
            target = run.refreshed.get(root)
            if target is not None and target.owner_root not in (None, self._root_uuid):
                run.request_stop("foreign-owner")
                return False
            run.names_by_parent.clear()
            if (drift := _first_drift(before, run.refreshed)) is not None:
                if run.drift_rel is None:
                    run.drift_rel = drift
                if run.create_folder_cache is not None:
                    run.create_folder_cache.clear()
        run.index_dirty = False
        return run.can_start()

    def _probe_created(self, run: _Run) -> None:
        if self._record_probe is not None and run.pending_probe:
            self._record_probe(run.pending_probe)
            run.pending_probe = []

    def _content(self, run: _Run, pending: deque[_Work]) -> None:
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=self._parallel) as pool:
            active: dict[Future[bool], _Work] = {}
            deferred: deque[_Work] = deque()
            while pending or active:
                while pending and len(active) < self._parallel and run.can_start():
                    # Wait for active writes before refreshing their shared snapshot.
                    if self._refresh_due(run) and (active or not self._refresh(run)):
                        break
                    work = pending.popleft()
                    run.ops_since_refresh += 1
                    active[pool.submit(self._execute_one, run, work)] = work
                if not active:
                    break
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    work = active.pop(future)
                    # result() also propagates a worker's BaseException to the caller.
                    if future.result():
                        deferred.append(work)
                if not pending and not active:
                    # Give changes to a local file the rest of this pass to settle.
                    pending, deferred = deferred, pending

    def _execute_one(self, run: _Run, work: _Work) -> bool:
        finished = False
        try:
            deferred = run.begin(self._index, work.op) and self._attempts(run, work)
            finished = True
            return deferred
        finally:
            if not finished:
                # Stop dispatch before the pool publishes an unexpected worker failure.
                run.aborted.set()

    def _attempts(self, run: _Run, work: _Work) -> bool:
        op = work.op
        file_id = op.file_id
        assert file_id is not None
        while True:
            try:
                self._check_index(run, op, file_id)
                item, uploaded, backup_md5 = self._apply(run, work, file_id)
                additions = []
                if op.kind is OpKind.TRASH and item.is_folder:
                    # If the listing fails, keep the confirmed write open for reconciliation.
                    work.trash_uncertain = True
                    additions = _audit_folder_trash(self._drive, self._index, op, self._stop.is_set)
            except _FolderKept as exc:
                self._failure(run, work, exc)
                run.report.warnings.append(PlanIssue("folder-kept-protected", op.rel))
                run.report.warnings.extend(exc.warnings)
                self._index.save_ops(run.id, exc.children)
                run.trash_followups.extend(_Work(child) for child in exc.children)
                return False
            except (
                AuthError,
                DriveError,
                FileChanged,
                Md5Mismatch,
                Stopped,
                _LocalReadError,
                RemoteChanged,
            ) as exc:
                retry = self._failure(run, work, exc)
                if op.kind in _UPLOAD:
                    self._tracker.transfer_done(op.rel)
                if retry is _Retry.IMMEDIATE:
                    if self._stop.is_set():
                        run.request_stop("cancelled")
                        return False
                    self._index.mark_inflight(op)
                    continue
                return retry is _Retry.DEFERRED

            with run.lock:
                if op.kind is OpKind.TRASH:
                    self._index.complete_op(
                        op,
                        remove_subtrees=[file_id],
                        trash_log_run=run.id,
                        trashed_is_folder=item.is_folder,
                        trash_additions=additions,
                    )
                    run.report.warnings.extend(
                        PlanIssue(TRASH_TOOK_NEW_ITEMS, entry.rel) for entry in additions
                    )
                else:
                    digest = backup_md5 if op.kind in _UPLOAD else op.md5
                    version = (
                        FileVersion(op.identity, digest)
                        if op.identity is not None
                        and digest is not None
                        and not item.trashed
                        and (item.id, item.size, item.md5) == (file_id, op.size, digest)
                        else None
                    )
                    self._index.complete_op(
                        op,
                        upsert=[item],
                        local_md5=backup_md5 if op.kind in _UPLOAD else None,
                        backed_up=(item.id, item.size, backup_md5)
                        if item.size is not None and backup_md5 is not None
                        else None,
                        file_version=version,
                    )
                run.report.done[op.kind] += 1
                run.report.uploaded_bytes += uploaded
                run.index_dirty = True
                if op.kind is OpKind.MKDIR:
                    run.created_folders.add(file_id)
                    if run.create_folder_cache is not None:
                        run.create_folder_cache.remember_directory(item)
                if op.kind is OpKind.UPLOAD and run.create_folder_cache is not None:
                    run.create_folder_cache.file_created()
                if (
                    op.kind is OpKind.MOVE
                    and item.is_folder
                    and run.create_folder_cache is not None
                ):
                    run.create_folder_cache.clear()
                if op.kind in _CREATE:
                    if len(run.report.created_ids) < 20:
                        run.report.created_ids.append(file_id)
                    if self._record_probe is not None and len(run.pending_probe) < 20:
                        run.pending_probe.append(file_id)
            if op.kind in _UPLOAD:
                self._tracker.transfer_done(op.rel)
            self._tracker.op_done(op.rel)
            return False

    def _check_index(self, run: _Run, op: Op, file_id: str) -> None:
        root = self._root_id
        current = self._index.get_remote(file_id)
        if current is not None and current.protected_from(self._root_uuid):
            raise ForeignItemKept(op.src_rel or op.rel)
        if root is None:
            return
        if op.kind not in _CREATE:
            planned = run.planned.get(file_id)
            if (
                not self._index.in_subtree(file_id, root)
                or file_id == root
                or planned is None
                or not _same_item(planned, current)
            ):
                raise RemoteChanged
            source = op.src_rel if op.kind is OpKind.MOVE else op.rel
            if (
                source is not None
                and run.refreshed is not None
                and run.refreshed.rel_of(file_id) != source
            ):
                raise RemoteChanged
        if op.kind in _WITH_PARENT:
            parent = op.parent_id
            assert parent is not None
            if not self._index.in_subtree(parent, root) and parent not in run.created_folders:
                raise RemoteChanged
            refreshed = run.refreshed
            if refreshed is None or (parent in run.created_folders and parent not in refreshed):
                return
            if op.parent_rel is not None and refreshed.rel_of(parent) != op.parent_rel:
                raise RemoteChanged
            if op.name is not None:
                for other_id in run.matching_children(parent, op.name):
                    if other_id == file_id:
                        continue
                    current = self._index.get_remote(other_id)
                    if (
                        current is not None
                        and not current.trashed
                        and parent in current.parents
                        and _rel_component(current.name) == op.name
                        and not current.is_google_native
                        and not current.protected_from(self._root_uuid)
                        and not _planned_type_conflict(run.planned, op, current)
                    ):
                        raise RemoteChanged

    def _check_destination(
        self, run: _Run, op: Op, file_id: str, cache: _CreateFolderCache | None = None
    ) -> None:
        parent = op.parent_id
        root = self._root_id
        if root is None or parent is None:
            return
        in_scope = cache.check(parent, op.parent_rel) if cache is not None else None
        if in_scope is None:
            in_scope = self._drive.live_folder_in_subtree(
                parent, root, rel=op.parent_rel, root_uuid=self._root_uuid
            )
        if not in_scope:
            raise RemoteChanged
        if op.name is not None:
            for item in self._drive.list_children([parent], name=op.name):
                if item.protected_from(self._root_uuid):
                    if item.id == file_id:
                        raise ForeignItemKept
                elif (
                    item.id != file_id
                    and not item.is_google_native
                    and not _planned_type_conflict(run.planned, op, item)
                ):
                    raise RemoteChanged

    def _check_live_parent(self, item: RemoteFile, rel: str | None) -> None:
        root = self._root_id
        if root is not None and (
            len(item.parents) != 1
            or not self._drive.live_folder_in_subtree(
                item.parents[0],
                root,
                rel=rel.rpartition("/")[0] if rel is not None else None,
                root_uuid=self._root_uuid,
            )
        ):
            raise RemoteChanged

    def _trash_file(self, item: RemoteFile, rel: str) -> TrashFile:
        return TrashFile(
            item.id,
            rel,
            item.size,
            item.md5,
            owned_by_mirror=item.owned_by_me is True and item.owner_root == self._root_uuid,
            is_folder=item.is_folder,
        )

    def _check_folder_trash(self, run: _Run, op: Op, folder: RemoteFile) -> None:
        expected: dict[str, tuple[RemoteFile, str]] = {}
        pending = [(folder.id, op.rel)]
        for parent, rel in pending:
            for child in run.planned.children(parent):
                if child.id in run.moving or child.is_google_native:
                    continue
                child_rel = rel_join(rel, _rel_component(child.name))
                expected[child.id] = child, child_rel
                if child.is_folder:
                    pending.append((child.id, child_rel))
        if sum(not child.is_folder for child, _ in expected.values()) != op.affected_files:
            raise RemoteChanged

        # Walk only confirmed planned folders. A foreign folder and its contents
        # are protected even when some planned file IDs have moved into it.
        folders = [(folder.id, op.rel)]
        covered: dict[str, RemoteFile] = {}
        blocked: set[str] = set()
        warnings = []
        for parent, rel in folders:
            for live in self._drive.list_children([parent]):
                match = expected.get(live.id)
                if live.protected_from(self._root_uuid):
                    warnings.append(
                        PlanIssue(FOREIGN_ITEM_KEPT, rel_join(rel, _rel_component(live.name)))
                    )
                    blocked.add(parent)
                    continue
                if match is None or live.parents != (parent,) or not _same_item(match[0], live):
                    blocked.add(parent)
                    continue
                covered[live.id] = live
                if live.is_folder:
                    folders.append((live.id, match[1]))
        if not blocked:
            self._index.record_trash_files(
                op,
                [
                    self._trash_file(folder, op.rel),
                    *(self._trash_file(item, expected[item.id][1]) for item in covered.values()),
                ],
                guarded_ids=covered.keys(),
            )
            return
        for child_id, _ in reversed(folders[1:]):
            if child_id in blocked:
                blocked.add(covered[child_id].parents[0])

        counts: Counter[str] = Counter()
        for child, _ in reversed(list(expected.values())):
            counts[child.parents[0]] += counts[child.id] + int(not child.is_folder)
        raise _FolderKept(
            [
                Op(
                    OpKind.TRASH,
                    expected[item.id][1],
                    op.reason,
                    file_id=item.id,
                    affected_files=counts[item.id] if item.is_folder else 1,
                )
                for item in covered.values()
                if item.id not in blocked and item.parents[0] in blocked
            ],
            warnings,
        )

    def _check_folder_move(self, folder: RemoteFile, source: str) -> None:
        pending = [(folder.id, source)]
        seen = {folder.id}
        for parent, rel in pending:
            for child in self._drive.list_children([parent]):
                child_rel = rel_join(rel, _rel_component(child.name))
                if child.protected_from(self._root_uuid):
                    raise ForeignItemKept(child_rel)
                if child.is_folder and child.id not in seen:
                    seen.add(child.id)
                    pending.append((child.id, child_rel))

    def _trash(self, run: _Run, work: _Work, file_id: str) -> RemoteFile:
        op = work.op
        planned = run.planned.get(file_id)
        if planned is None:
            raise RemoteChanged
        sent = op.db_id in run.unconfirmed_trash

        def before_send() -> None:
            nonlocal sent
            live = self._drive.get(file_id)
            if live is not None and live.id == file_id and live.trashed and sent:
                raise _TrashConfirmed(live)
            if live is not None and live.protected_from(self._root_uuid):
                raise ForeignItemKept
            if live is None and sent:
                work.trash_uncertain = True
            if live is None or not _same_item(planned, live):
                raise RemoteChanged
            self._check_live_parent(live, op.rel)
            # Persist before sending so reconciliation can use the original metadata.
            if live.is_folder:
                self._check_folder_trash(run, op, live)
            else:
                self._index.record_trash_files(op, [self._trash_file(live, op.rel)])
            sent = True

        try:
            item = self._drive.update_metadata(
                file_id, trashed=True, before_send=before_send, should_stop=self._stop.is_set
            )
            if item.id == file_id and item.trashed:
                # The direct response may have newer metadata than the guard saw.
                confirmed = self._trash_file(item, op.rel)
                if confirmed != self._trash_file(planned, op.rel):
                    self._index.confirm_trash_file(op, confirmed)
                return item
            raise DriveError("Drive did not confirm trash")
        except _TrashConfirmed as confirmed:
            return confirmed.item
        except (DriveError, AuthError) as error:
            if sent and (
                not isinstance(error, DriveError) or error.retryable or error.status < 400
            ):
                work.trash_uncertain = True
                try:
                    live = self._drive.get(file_id)
                except (DriveError, AuthError):
                    pass  # Keep the durable intent for the next run to reconcile.
                else:
                    if live is not None and live.id == file_id and live.trashed:
                        return live
                    work.trash_uncertain = live is None
            raise

    def _apply(self, run: _Run, work: _Work, file_id: str) -> tuple[RemoteFile, int, str | None]:
        op = work.op
        if op.kind in _UPLOAD:
            # A deferred or retried operation may already have created its Drive ID.
            cache = run.create_folder_cache if not (work.deferrals or work.md5_retries) else None
            return self._upload(run, op, file_id, cache)
        if op.kind is OpKind.TRASH:
            return self._trash(run, work, file_id), 0, None
        stop = self._stop.is_set

        def check_metadata() -> None:
            self._check_destination(run, op, file_id)
            live = self._drive.get(file_id)
            source = op.src_rel if op.kind is OpKind.MOVE else op.rel
            if live is not None and live.protected_from(self._root_uuid):
                raise ForeignItemKept(source or op.rel)
            if self._root_id is None:
                if op.kind is OpKind.MOVE and live is not None and live.is_folder:
                    self._check_folder_move(live, source or op.rel)
                return
            previous = run.planned.get(file_id)
            if previous is None or live is None:
                raise RemoteChanged
            if not _same_item(previous, live):
                # An earlier retryable request may already have changed the item.
                if not (
                    not live.trashed
                    and live.mime_type == previous.mime_type
                    and (live.size, live.md5) == (previous.size, previous.md5)
                    and live.name == (op.name if op.kind is OpKind.MOVE else previous.name)
                    and live.parents
                    == ((op.parent_id,) if op.kind is OpKind.MOVE else previous.parents)
                    and (live.is_folder or live.modified_ms == drive_ms(op.mtime_ns))
                    and live.owner_root == self._properties[OWNER_PROPERTY]
                ):
                    raise RemoteChanged
                source = op.rel
            self._check_live_parent(live, source)
            if op.kind is OpKind.MOVE and live.is_folder:
                self._check_folder_move(live, source or op.rel)

        if op.kind is OpKind.MKDIR:
            assert op.parent_id is not None and op.name is not None
            cache = run.create_folder_cache

            def check_mkdir() -> None:
                nonlocal cache
                self._check_destination(run, op, file_id, cache)
                cache = None

            item = self._drive.create_folder(
                file_id=file_id,
                name=op.name,
                parent_id=op.parent_id,
                properties=self._properties,
                before_send=check_mkdir,
                should_stop=stop,
            )
        elif op.kind is OpKind.MOVE:
            previous = self._index.get_remote(file_id)
            is_folder = previous.is_folder if previous is not None else op.mtime_ns == 0
            add_parent = op.parent_id if op.parent_id != op.src_parent_id else None
            item = self._drive.update_metadata(
                file_id,
                name=op.name,
                modified_ms=None if is_folder else drive_ms(op.mtime_ns),
                properties=self._properties,
                add_parent=add_parent,
                remove_parents=[op.src_parent_id] if add_parent and op.src_parent_id else (),
                before_send=check_metadata,
                should_stop=stop,
            )
        else:
            item = self._drive.update_metadata(
                file_id,
                modified_ms=drive_ms(op.mtime_ns),
                properties=self._properties,
                before_send=check_metadata,
                should_stop=stop,
            )
        previous = run.planned.get(file_id)
        # TOUCH also adopts an unmanaged file when its bytes match but its mtime differs.
        adopted = (
            op.kind in (OpKind.ADOPT, OpKind.TOUCH)
            and op.md5 is not None
            and previous is not None
            and previous.owner_root is None
            and previous.md5 == op.md5
            and item.id == file_id
            and item.owner_root == self._root_uuid
            and (item.size, item.md5) == (previous.size, op.md5)
        )
        return item, 0, op.md5 if adopted else None

    def _upload(
        self, run: _Run, op: Op, file_id: str, cache: _CreateFolderCache | None
    ) -> tuple[RemoteFile, int, str]:
        def on_bytes(delta: int) -> None:
            try:
                self._tracker.transfer(op.rel, delta, op.size)
            except OSError as exc:
                # A progress write failure is unrelated to reading the source.
                raise _ProgressError("Could not publish transfer progress") from exc

        update = op.kind is OpKind.UPDATE
        request = UploadRequest(
            rel=op.rel,
            path=self._source_root / (op.disk_rel or op.rel),
            size=op.size,
            mtime_ns=op.mtime_ns,
            file_id=file_id,
            update=update,
            name=None if update else op.name,
            parent_id=None if update else op.parent_id,
            properties=self._properties,
            source_root=self._real_source_root,
            destination_rel=op.rel,
            expected_remote=run.planned.get(file_id) if update else None,
            known_type_conflict=lambda item: _planned_type_conflict(run.planned, op, item),
            identity=op.identity,
        )
        try:
            if self._root_id is None and update:
                live = self._drive.get(file_id)
                if live is not None and live.protected_from(self._root_uuid):
                    raise ForeignItemKept
            with self._drive.guard_upload_scope(self._root_id or "", self._root_uuid):
                outcome = self._uploader.upload(
                    request,
                    on_bytes=on_bytes,
                    should_stop=self._stop.is_set,
                    root_id=self._root_id,
                    check_create_parent=cache.check if cache is not None and not update else None,
                )
        except FileNotFoundError as exc:
            raise _LocalReadError(code="local_vanished") from exc
        except OSError as exc:
            raise _LocalReadError(code="local_unreadable") from exc
        return outcome.file, outcome.bytes_sent, outcome.local_md5

    def _failure(self, run: _Run, work: _Work, error: NimboError) -> _Retry | None:
        op = work.op
        if isinstance(error, Stopped):
            run.request_stop("cancelled")
            return None

        code = error.code
        if isinstance(error, RemoteChanged) and run.create_folder_cache is not None:
            run.create_folder_cache.clear()
            run.refresh_requested.set()
        if isinstance(error, FileChanged):
            if op.phase is Phase.CONTENT and work.deferrals + 1 < self._changed_retries:
                return self._defer(work)
            code = "file_in_use"
        elif isinstance(error, Md5Mismatch):
            if work.md5_retries < self._md5_retries:
                work.md5_retries += 1
                return _Retry.IMMEDIATE
            code = "md5_mismatch"
        elif isinstance(error, NotFound):
            code = "remote_missing"
        elif op.kind is OpKind.MOVE and isinstance(error, (BadRequest, Forbidden)):
            if work.deferrals == 0:
                return self._defer(work)
            code = "move_rejected"

        if (reason := _stop_reason(error)) is not None:
            run.request_stop(reason)
        with run.lock:
            if code == FOREIGN_ITEM_KEPT:
                run.report.warnings.append(PlanIssue(FOREIGN_ITEM_KEPT, error.detail or op.rel))
            # A previous HTTP attempt can have committed even if its reply was lost.
            # Do not rely on the feed to find an unconfirmed write before the next plan.
            self._index.set_meta(META_FORCE_FULL, "1")
            if not work.trash_uncertain:
                self._index.fail_op(op, code)
            run.report.failed.append((op.rel, code))
        return None

    def _defer(self, work: _Work) -> _Retry:
        # A deferred op can remain pending at shutdown, including after a write
        # that succeeded just before the uploader noticed a source change.
        self._index.set_meta(META_FORCE_FULL, "1")
        self._index.reset_op(work.op)
        work.deferrals += 1
        return _Retry.DEFERRED
