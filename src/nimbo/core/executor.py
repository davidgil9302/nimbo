"""Apply a persisted mirror plan, with a durable intent before each Drive write."""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from threading import Event, Lock

from .drive import DriveClient
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
    ServerError,
    Stopped,
    StorageQuotaExceeded,
    TransportFailed,
    Unauthorized,
    UploadLimitExceeded,
)
from .index import META_FORCE_FULL, Index
from .model import OWNER_PROPERTY, Op, OpKind, Phase, RemoteFile, drive_ms
from .progress import ProgressTracker
from .transfer import Uploader, UploadRequest

_CREATE = {OpKind.MKDIR, OpKind.UPLOAD}
_WITH_PARENT = _CREATE | {OpKind.MOVE}
_UPLOAD = {OpKind.UPLOAD, OpKind.UPDATE}
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


@dataclass(slots=True)
class _Run:
    id: int
    stop: Event
    report: ExecReport = field(default_factory=lambda: ExecReport(Counter(), 0, [], None, []))
    lock: Lock = field(default_factory=Lock)
    aborted: Event = field(default_factory=Event)
    created_folders: set[str] = field(default_factory=set)
    pending_probe: list[str] = field(default_factory=list)
    last_refresh: float | None = None

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


@dataclass(slots=True)
class _Work:
    op: Op
    deferrals: int = 0
    md5_retries: int = 0


class _Retry(Enum):
    IMMEDIATE = auto()
    DEFERRED = auto()


class _LocalReadError(NimboError):
    pass


class _RemoteChanged(NimboError):
    code = "remote_changed"


class _ProgressError(RuntimeError):
    pass


def _stop_reason(error: NimboError) -> str | None:
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
        clock: Callable[[], float] = time.monotonic,
        record_probe: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        """Bind the run's dependencies and set retry budgets.

        Guard writes under `root_id` when supplied. Refresh the mirror on the
        scheduling thread before phases and after the given interval. Send
        newly created IDs to `record_probe` before consuming their feed window.
        Raise ValueError for invalid retry budgets, parallelism or refresh interval.
        """
        if parallel < 1 or changed_retries < 1 or md5_retries < 0:
            raise ValueError("Expected positive parallel and changed_retries, and md5_retries >= 0")
        if not math.isfinite(refresh_every) or refresh_every < 0:
            raise ValueError("refresh_every must be finite and nonnegative")
        self._drive = drive
        self._uploader = uploader
        self._index = index
        self._source_root = source_root
        self._properties = {OWNER_PROPERTY: root_uuid}
        self._tracker = tracker
        self._stop = stop
        self._parallel = parallel
        self._changed_retries = changed_retries
        self._md5_retries = md5_retries
        self._root_id = root_id
        self._refresh_remote = refresh_remote
        self._refresh_every = refresh_every
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

        run = _Run(run_id, self._stop)
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
                    if self._execute_one(run, work):
                        pending.append(work)

        run.can_start()  # Observe cancellation even if the final operation completed.
        self._tracker.flush(force=True)
        self._probe_created(run)
        return run.report

    def _refresh_due(self, run: _Run) -> bool:
        return (
            self._refresh_remote is not None
            and run.last_refresh is not None
            and self._clock() - run.last_refresh >= self._refresh_every
        )

    def _refresh(self, run: _Run) -> bool:
        if self._refresh_remote is None:
            return True
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
        previous = self._index.get_remote(file_id) if op.kind is OpKind.TRASH else None
        while True:
            try:
                self._check_scope(run, op, file_id, previous)
                item, uploaded = self._apply(op, file_id)
            except (
                AuthError,
                DriveError,
                FileChanged,
                Md5Mismatch,
                Stopped,
                _LocalReadError,
                _RemoteChanged,
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
                        trashed_is_folder=(previous if previous is not None else item).is_folder,
                    )
                else:
                    self._index.complete_op(op, upsert=[item])
                run.report.done[op.kind] += 1
                run.report.uploaded_bytes += uploaded
                if op.kind is OpKind.MKDIR:
                    run.created_folders.add(file_id)
                if op.kind in _CREATE:
                    if len(run.report.created_ids) < 20:
                        run.report.created_ids.append(file_id)
                    if self._record_probe is not None and len(run.pending_probe) < 20:
                        run.pending_probe.append(file_id)
            if op.kind in _UPLOAD:
                self._tracker.transfer_done(op.rel)
            self._tracker.op_done(op.rel)
            return False

    def _check_scope(self, run: _Run, op: Op, file_id: str, previous: RemoteFile | None) -> None:
        root = self._root_id
        if root is None:
            return
        if op.kind not in _CREATE and not self._index.in_subtree(file_id, root):
            raise _RemoteChanged
        if op.kind in _WITH_PARENT:
            parent = op.parent_id
            assert parent is not None
            if not self._index.in_subtree(parent, root) and not (
                parent in run.created_folders and self._live_folder_in_subtree(parent, root)
            ):
                raise _RemoteChanged
        if op.kind is OpKind.TRASH:
            live = self._drive.get(file_id)
            if live is None or live.trashed:
                raise _RemoteChanged
            if previous is None or previous.parents != live.parents:
                if not self._refresh(run):
                    raise Stopped
                live = self._drive.get(file_id)
                previous = self._index.get_remote(file_id)
                if (
                    live is None
                    or live.trashed
                    or previous is None
                    or previous.parents != live.parents
                    or not self._index.in_subtree(file_id, root)
                ):
                    raise _RemoteChanged
            # An ancestor can move without changing this item's direct parent.
            if len(live.parents) != 1 or not self._live_folder_in_subtree(live.parents[0], root):
                raise _RemoteChanged

    def _live_folder_in_subtree(self, folder_id: str, root: str) -> bool:
        seen = set()
        while folder_id != root:
            if folder_id in seen:
                return False
            seen.add(folder_id)
            folder = self._drive.get(folder_id)
            if folder is None or folder.trashed or not folder.is_folder or len(folder.parents) != 1:
                return False
            folder_id = folder.parents[0]
        return True

    def _apply(self, op: Op, file_id: str) -> tuple[RemoteFile, int]:
        if op.kind in _UPLOAD:
            return self._upload(op, file_id)
        if op.kind is OpKind.MKDIR:
            assert op.parent_id is not None and op.name is not None
            item = self._drive.create_folder(
                file_id=file_id, name=op.name, parent_id=op.parent_id, properties=self._properties
            )
        elif op.kind is OpKind.TRASH:
            item = self._drive.update_metadata(file_id, trashed=True)
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
            )
        else:
            item = self._drive.update_metadata(
                file_id, modified_ms=drive_ms(op.mtime_ns), properties=self._properties
            )
        return item, 0

    def _upload(self, op: Op, file_id: str) -> tuple[RemoteFile, int]:
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
        )
        try:
            outcome = self._uploader.upload(
                request, on_bytes=on_bytes, should_stop=self._stop.is_set
            )
        except FileNotFoundError as exc:
            raise _LocalReadError(code="local_vanished") from exc
        except OSError as exc:
            raise _LocalReadError(code="local_unreadable") from exc
        return outcome.file, outcome.bytes_sent

    def _failure(self, run: _Run, work: _Work, error: NimboError) -> _Retry | None:
        op = work.op
        if isinstance(error, Stopped):
            run.request_stop("cancelled")
            return None

        code = error.code
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
            # A previous HTTP attempt can have committed even if its reply was lost.
            # Once FAILED, the op is no longer reconciled by ID. Do not rely on the
            # changes feed to find an unconfirmed create before the next plan.
            self._index.set_meta(META_FORCE_FULL, "1")
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
