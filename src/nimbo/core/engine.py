"""Orchestrate journaled backup runs, previews, restorations, and audits."""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Protocol

from .allocate import allocate
from .config import Config
from .drive import DriveClient
from .errors import AuthError, LockedError, Md5Mismatch, NimboError, Stopped
from .executor import TRASH_TOOK_NEW_ITEMS, ExecReport, Executor, _audit_folder_trash
from .hasher import LocalHasher
from .index import (
    META_ACCOUNT,
    META_CHANGES_PROBE,
    META_CHANGES_RELIABLE,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    META_LAST_DEEP,
    META_LAST_FULL,
    META_QUOTA_LIMIT,
    META_QUOTA_USED,
    META_ROOT_UUID,
    META_TARGET_ID,
    META_UNDO_BOUNDARY,
    Index,
    TrashFile,
)
from .lock import RunLock
from .model import (
    FOREIGN_ITEM_KEPT,
    INVALID_NAME_PREFIX,
    About,
    AuditReport,
    BrakeDecision,
    FolderStat,
    Op,
    OpKind,
    OpState,
    Plan,
    PlanIssue,
    PlanStats,
    Preview,
    RemoteFile,
    RunResult,
    RunSummary,
    ScanProblem,
    ScanResult,
    UndoSummary,
    nfc,
    rel_join,
    rel_parent,
    rel_top,
)
from .paths import Paths
from .planner import PlannerOptions, make_plan
from .progress import ProgressTracker
from .remote_sync import RemoteIndexer
from .safety import (
    OVERWRITE_CONFIRMATION,
    check_source,
    check_space,
    consume_approval,
    evaluate_brakes,
    network_available,
    read_sentinel,
)
from .scanner import scan
from .transfer import Downloader, Uploader, create_session_kind

_LOG = logging.getLogger("nimbo.engine")
_WEEK = timedelta(days=7)
_CONTENT_WRITES = {OpKind.UPLOAD, OpKind.UPDATE}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Notifier(Protocol):
    """Receive a notification once a run summary has been stored."""

    def run_finished(self, summary: RunSummary, *, manual: bool) -> None:
        """Handle a finished run, with manual=False for timer runs."""
        ...


@dataclass
class EngineDeps:
    """Supply the configured source and target, state, transfers, and runtime hooks."""

    cfg: Config
    paths: Paths
    index: Index
    drive: DriveClient
    uploader: Uploader
    downloader: Downloader
    notifier: Notifier
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    network_check: Callable[[], bool] = network_available
    idle_inhibitor: Callable[[], AbstractContextManager[None]] = nullcontext


@dataclass(slots=True)
class ScanAuditReport(AuditReport):
    """Report mirror differences and local paths that could not be audited."""

    scan_problems: list[ScanProblem] = field(default_factory=list)


@dataclass(slots=True)
class _Prepared:
    root_uuid: str
    about: About
    indexer: RemoteIndexer
    scanned: ScanResult
    plan: Plan
    brake: BrakeDecision
    deep: bool


class _PreflightError(NimboError):
    def __init__(
        self, code: str, detail: str = "", *, result: RunResult = RunResult.SKIPPED
    ) -> None:
        super().__init__(detail, code=code)
        self.result = result


def _check_stop(stop: Event) -> None:
    if stop.is_set():
        raise Stopped


def _phase(progress: ProgressTracker | None, phase: str) -> None:
    if progress is not None:
        progress.phase(phase)


def _internal_error(summary: RunSummary) -> None:
    _LOG.exception("Backup run failed")
    summary.result = RunResult.FAILED
    summary.reason = "internal_error"
    summary.detail = ""
    summary.fingerprint = None
    summary.trash_preview.clear()


def _restore_parts(rel: str) -> tuple[str, ...]:
    parts = tuple(rel.split("/"))
    if any(
        part in ("", ".", "..") or "\x00" in part or part.startswith(INVALID_NAME_PREFIX)
        for part in parts
    ):
        raise NimboError(code="unsafe_restore_path")
    return parts


def _restore_name(parent_fd: int, component: str) -> str:
    canonical = nfc(component)
    matches = [name for name in os.listdir(parent_fd) if nfc(name) == canonical]
    if len(matches) > 1:
        raise NimboError(code="unsafe_restore_path")
    return matches[0] if matches else canonical


def _open_restore_dir(parent_fd: int, name: str) -> tuple[int, bool]:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd), False
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd), False

    # O_PATH can pin the new directory even when the umask removed owner access.
    created_fd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        Path(f"/proc/self/fd/{created_fd}").chmod(0o700)
        return os.open(".", _DIR_FLAGS, dir_fd=created_fd), True
    finally:
        os.close(created_fd)


@contextmanager
def _restore_parent(root_fd: int, rel: str) -> Iterator[tuple[int, str]]:
    parts = _restore_parts(rel)
    # Walk actual directory entries without following symlinks, even if one is
    # swapped in between the resolve check and the open. Pin each opened parent.
    parent_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            name = _restore_name(parent_fd, component)
            child_fd, _ = _open_restore_dir(parent_fd, name)
            os.close(parent_fd)
            parent_fd = child_fd
        yield parent_fd, _restore_name(parent_fd, parts[-1])
    finally:
        os.close(parent_fd)


class Engine:
    """Serialize operations on a source and Drive mirror using a shared run lock."""

    def __init__(self, deps: EngineDeps) -> None:
        """Bind the supplied configuration and dependencies for all engine operations."""
        self._deps = deps

    def run(
        self, *, trigger: str, allow_brakes: bool = False, stop: Event | None = None
    ) -> RunSummary:
        """Run the mirror and store its outcome, or return a skip if another run holds the lock.

        Cancellation is reported in the summary. Exceptions outside Python's
        Exception hierarchy propagate and leave the journal available for recovery.
        """
        deps = self._deps
        summary = RunSummary(None, RunResult.FAILED, trigger=trigger)
        lock = RunLock(deps.paths.lock_file)
        try:
            lock.acquire()
        except LockedError:
            summary.result, summary.reason = RunResult.SKIPPED, "already_running"
            return summary
        except Exception:  # noqa: BLE001 - the run entry point reports unexpected failures
            _internal_error(summary)
            return summary
        try:
            self._run_locked(summary, allow_brakes, stop if stop is not None else Event())
        finally:
            try:
                lock.release()
            except Exception:  # noqa: BLE001 - preserve a propagating crash during cleanup
                _internal_error(summary)
                if summary.run_id is not None and summary.finished_at:
                    try:
                        deps.index.finish_run(summary)
                    except Exception:  # noqa: BLE001 - the previously stored result is retained
                        _internal_error(summary)
        return summary

    def _run_locked(self, summary: RunSummary, allow_brakes: bool, stop: Event) -> None:
        deps = self._deps
        progress = None
        try:
            summary.started_at = deps.clock().isoformat()
            run_id = deps.index.start_run(summary.trigger, summary.started_at)
            summary.run_id = run_id
            progress = ProgressTracker(deps.paths.progress_file, run_id)
            _phase(progress, "preflight")
            prepared = self._prepare(
                stop, summary, progress, force_deep=summary.trigger == "verify"
            )
            self._execute(prepared, run_id, summary, allow_brakes, stop, progress)
        except _PreflightError as exc:
            summary.result, summary.reason, summary.detail = exc.result, exc.code, exc.detail
        except Stopped:
            summary.result, summary.reason = RunResult.CANCELLED, "cancelled"
        except Exception:  # noqa: BLE001 - the run entry point reports unexpected failures
            _internal_error(summary)
        finally:
            if progress is not None:
                try:
                    progress.close()
                except Exception:  # noqa: BLE001 - cleanup must also preserve a propagating crash
                    _internal_error(summary)

        if summary.run_id is not None:
            self._finish(summary)

    def _finish(self, summary: RunSummary) -> None:
        deps = self._deps
        try:
            if not summary.finished_at:
                summary.finished_at = deps.clock().isoformat()
            deps.index.finish_run(summary)
        except Exception:  # noqa: BLE001 - a transient failure may still permit recording the result
            _internal_error(summary)
            summary.finished_at = summary.finished_at or summary.started_at
            try:
                deps.index.finish_run(summary)
            except Exception:  # noqa: BLE001 - the index is unavailable
                _internal_error(summary)
                return

        try:
            deps.notifier.run_finished(summary, manual=summary.trigger != "timer")
        except Exception:  # noqa: BLE001 - record notifier failures in the run outcome
            _internal_error(summary)
            try:
                deps.index.finish_run(summary)
            except Exception:  # noqa: BLE001 - the previously stored summary remains available
                _internal_error(summary)

    def _root_uuid(self, *, restoring: bool = False, auditing: bool = False) -> str:
        deps = self._deps
        root_uuid = read_sentinel(deps.cfg.source.root)
        if not root_uuid:
            raise _PreflightError("sentinel_missing")
        previous = deps.index.get_meta(META_ROOT_UUID)
        if previous and previous != root_uuid:
            raise _PreflightError("sentinel_mismatch")
        if not previous:
            deps.index.set_meta(META_ROOT_UUID, root_uuid)
        ignored = {"folder_unreadable"}
        if restoring:
            ignored.update(("folder_missing", "folder_empty"))
        elif auditing:
            ignored.add("folder_empty")
        for problem in check_source(deps.cfg, root_uuid):
            if problem.code not in ignored:
                raise _PreflightError(problem.code, problem.detail)
        return root_uuid

    def _reset_target(self) -> None:
        deps = self._deps
        target = deps.cfg.target.folder_id
        previous = deps.index.get_meta(META_TARGET_ID)
        if target == previous:
            return
        if previous:
            # An open create session carries its original parent in Drive. The
            # session key has no target field, so retire it before switching targets.
            deps.index.prune_sessions(datetime.max.replace(tzinfo=UTC).isoformat())
        deps.index.replace_remote(
            [],
            meta={
                META_TARGET_ID: target,
                META_CHANGES_TOKEN: None,
                META_CHANGES_PROBE: None,
                META_CHANGES_RELIABLE: None,
                META_LAST_FULL: None,
                META_LAST_DEEP: None,
            },
        )

    def _reconcile(self, summary: RunSummary | None, stop: Event) -> None:
        deps = self._deps
        inflight = deps.index.inflight_ops()
        if inflight:
            # Keep the next refresh authoritative even if interrupted after
            # abandoning intents. The old cursor can replay older metadata.
            deps.index.set_meta(META_FORCE_FULL, "1")
        abandoned = []
        for op in inflight:
            _check_stop(stop)
            if op_id := op.file_id:
                item = deps.drive.get(op_id)
                if op.kind is OpKind.TRASH:
                    # Preview has no run ID for the undo log. Keep its trash
                    # intents until a backup can journal their reconciled outcome.
                    if summary is None:
                        continue
                    if item is not None and item.trashed:
                        additions = (
                            _audit_folder_trash(deps.drive, deps.index, op, stop.is_set)
                            if item.is_folder
                            else []
                        )
                        deps.index.complete_op(
                            op,
                            remove_subtrees=[op_id],
                            trash_log_run=summary.run_id,
                            trashed_is_folder=item.is_folder,
                            trash_additions=additions,
                        )
                        summary.warnings.extend(
                            PlanIssue(TRASH_TOOK_NEW_ITEMS, entry.rel) for entry in additions
                        )
                        continue
                    if item is None:
                        # Gone for good (or no longer reachable): undo could not
                        # restore it, so complete the intent without an undo entry.
                        deps.index.complete_op(op, remove_subtrees=[op_id])
                        continue
                elif item is None or item.trashed:
                    deps.index.delete_remote([op_id])
                else:
                    deps.index.upsert_remote([item])
            if op.db_id is not None:
                abandoned.append(op.db_id)
        if abandoned:
            deps.index.abandon_ops(abandoned)

    def _indexer(self, stop: Event) -> RemoteIndexer:
        deps = self._deps
        return RemoteIndexer(
            deps.drive,
            deps.index,
            deps.cfg.target.folder_id,
            clock=deps.clock,
            should_stop=stop.is_set,
        )

    def _prepare(
        self,
        stop: Event,
        summary: RunSummary | None = None,
        progress: ProgressTracker | None = None,
        *,
        force_deep: bool = False,
    ) -> _Prepared:
        deps = self._deps
        cfg, index = deps.cfg, deps.index
        _check_stop(stop)
        root_uuid = self._root_uuid()
        if not deps.network_check():
            raise _PreflightError("network_unavailable")
        _check_stop(stop)
        try:
            about = deps.drive.about()
        except AuthError as exc:
            raise _PreflightError(exc.code, exc.detail, result=RunResult.FAILED) from exc
        recorded_account = index.get_meta(META_ACCOUNT)
        if recorded_account is None:
            index.set_meta(META_ACCOUNT, about.email)
        elif recorded_account != about.email:
            raise _PreflightError("account_changed", result=RunResult.FAILED)
        index.set_meta(META_QUOTA_LIMIT, None if about.limit is None else str(about.limit))
        index.set_meta(META_QUOTA_USED, str(about.usage))
        self._reset_target()
        _phase(progress, "listing")
        self._reconcile(summary, stop)
        previous = index.load_tree(cfg.target.folder_id)
        indexer = self._indexer(stop)
        try:
            indexer.refresh()
        except NimboError as exc:
            if exc.code != "target_missing":
                raise
            raise _PreflightError(exc.code, exc.detail, result=RunResult.FAILED) from exc

        _phase(progress, "scanning")
        matcher = cfg.matcher()
        scanned = scan(cfg.source.root, cfg.active_folders, matcher, should_stop=stop.is_set)
        if summary is not None:
            summary.warnings.extend(
                PlanIssue(
                    problem.code,
                    problem.rel.encode("utf-8", "backslashreplace").decode("utf-8"),
                    problem.detail,
                )
                for problem in scanned.problems
            )
        if scanned.missing_folders:
            raise _PreflightError("folder_missing", scanned.missing_folders[0])
        if scanned.empty_folders:
            # Recheck physical emptiness after scanning to catch a mount or input
            # lost since preflight. A directory containing only exclusions can be valid.
            for problem in check_source(cfg, root_uuid):
                if problem.code != "folder_unreadable" or problem.detail not in scanned.protected:
                    raise _PreflightError(problem.code, problem.detail)

        _check_stop(stop)
        _phase(progress, "planning")
        deep = force_deep or self._deep_due()
        hasher = LocalHasher(cfg.source.root, index, should_stop=stop.is_set)

        def mapped_id(rel: str, candidates: set[str]) -> str | None:
            known = [file_id for file_id in candidates if previous.rel_of(file_id) == rel]
            if len(known) == 1:
                return known[0]
            return index.last_written_id(rel, candidates)

        plan = make_plan(
            scanned.entries,
            index.load_tree(cfg.target.folder_id),
            matcher,
            lambda entry: hasher.md5(entry, force=deep),
            PlannerOptions(cfg.active_folders, root_uuid, deep, tuple(scanned.protected)),
            verify_same_metadata=True,
            mapped_id=mapped_id,
        )

        def update_digest(op: Op) -> str:
            # An approval must cover the bytes even when size and mtime stayed fixed.
            digest = hasher.md5(scanned.entries[op.rel], force=True)
            if digest is None:
                raise _PreflightError("file_in_use", op.rel, result=RunResult.FAILED)
            return digest

        brake = evaluate_brakes(plan, cfg.safety, update_digest=update_digest)
        _check_stop(stop)
        return _Prepared(root_uuid, about, indexer, scanned, plan, brake, deep)

    def _deep_due(self) -> bool:
        deps = self._deps
        previous = deps.index.get_meta(META_LAST_DEEP)
        if not previous:
            return True
        try:
            age = deps.clock() - datetime.fromisoformat(previous)
        except (TypeError, ValueError):
            return True
        return age >= _WEEK or age < timedelta()

    def _execute(
        self,
        prepared: _Prepared,
        run_id: int,
        summary: RunSummary,
        allow_brakes: bool,
        stop: Event,
        progress: ProgressTracker,
    ) -> None:
        deps = self._deps
        plan = prepared.plan
        summary.warnings.extend(plan.warnings)
        if plan.errors:
            problem = plan.errors[0]
            raise _PreflightError(
                problem.code, problem.detail or problem.rel, result=RunResult.FAILED
            )
        brake = prepared.brake
        if brake.tripped and not (
            allow_brakes
            or consume_approval(deps.paths.approval_file, brake.fingerprint, now=deps.clock())
        ):
            summary.result = RunResult.NEEDS_CONFIRMATION
            summary.fingerprint = brake.fingerprint
            if plan.stats.updates:
                summary.reason = OVERWRITE_CONFIRMATION
            trashes = sorted(
                (op for op in plan.ops if op.kind is OpKind.TRASH), key=lambda op: op.rel
            )
            summary.trash_preview = [f"{op.rel}\t{op.affected_files}" for op in trashes[:500]]
            self._folder_stats(prepared, deps.clock().isoformat())
            return
        if problem_space := check_space(plan, prepared.about):
            self._folder_stats(prepared, deps.clock().isoformat())
            raise _PreflightError(problem_space.code, problem_space.detail, result=RunResult.FAILED)

        _check_stop(stop)
        allocate(
            plan,
            deps.cfg.target.folder_id,
            deps.drive.generate_ids,
            lambda op: self._reuse_upload_id(op, prepared.root_uuid),
        )
        _check_stop(stop)
        deps.index.save_ops(run_id, plan.ops)
        executor = Executor(
            drive=deps.drive,
            uploader=deps.uploader,
            index=deps.index,
            source_root=deps.cfg.source.root,
            root_uuid=prepared.root_uuid,
            tracker=progress,
            stop=stop,
            parallel=deps.cfg.transfer.parallel_uploads,
            root_id=deps.cfg.target.folder_id,
            refresh_remote=prepared.indexer.apply_changes,
            record_probe=prepared.indexer.record_probe,
        )
        with deps.idle_inhibitor():
            report = executor.execute(run_id, plan.ops)

        self._record_report(summary, report)
        if prepared.scanned.problems and summary.result in (RunResult.OK, RunResult.PARTIAL):
            summary.result, summary.reason = RunResult.PARTIAL, "scan_incomplete"
        now = deps.clock()
        summary.finished_at = now.isoformat()
        self._folder_stats(prepared, summary.finished_at, summary)
        if prepared.deep:
            deps.index.set_meta(META_LAST_DEEP, summary.finished_at)
        if summary.result is RunResult.OK:
            deps.index.prune_hashes(keep=prepared.scanned.entries.keys())
        deps.index.prune_sessions(older_than=(now - _WEEK).isoformat())

    def _reuse_upload_id(self, op: Op, root_uuid: str) -> str | None:
        if op.kind is OpKind.UPLOAD and op.parent_id is not None:
            key = (op.rel, op.size, op.mtime_ns, create_session_kind(op.parent_id))
            session = self._deps.index.get_session(*key)
            if session is not None:
                file_id, _ = session
                item = self._deps.drive.get(file_id)
                if item is None or (
                    not item.trashed
                    and not item.is_folder
                    and not item.protected_from(root_uuid)
                    and item.parents == (op.parent_id,)
                    and item.name == op.name
                ):
                    return file_id
                # A committed create can leave its session in SQLite after a crash.
                # If that file subsequently moved away, the plan needs a new ID.
                self._deps.index.delete_session(*key)
        return None

    @staticmethod
    def _record_report(summary: RunSummary, report: ExecReport) -> None:
        summary.warnings = list(dict.fromkeys([*summary.warnings, *report.warnings]))
        summary.mkdirs = report.done[OpKind.MKDIR]
        summary.uploaded = report.done[OpKind.UPLOAD]
        summary.uploaded_bytes = report.uploaded_bytes
        summary.updated = report.done[OpKind.UPDATE]
        summary.touched = report.done[OpKind.TOUCH]
        summary.moved = report.done[OpKind.MOVE]
        summary.adopted = report.done[OpKind.ADOPT]
        summary.trashed = report.done[OpKind.TRASH]
        summary.failed = sorted(report.failed)
        if report.stop_reason:
            summary.reason = report.stop_reason
            summary.result = {
                "cancelled": RunResult.CANCELLED,
                "upload_limit_exceeded": RunResult.PAUSED,
            }.get(report.stop_reason, RunResult.FAILED)
        elif report.failed or any(
            issue.code in (FOREIGN_ITEM_KEPT, TRASH_TOOK_NEW_ITEMS) for issue in summary.warnings
        ):
            summary.result = RunResult.PARTIAL
        else:
            summary.result = RunResult.OK

    def _folder_stats(
        self, prepared: _Prepared, now: str, summary: RunSummary | None = None
    ) -> None:
        deps = self._deps
        index = deps.index
        local_files: Counter[str] = Counter()
        local_bytes: Counter[str] = Counter()
        for entry in prepared.scanned.entries.values():
            if not entry.is_dir:
                folder = rel_top(entry.rel)
                local_files[folder] += 1
                local_bytes[folder] += entry.size

        planned = prepared.plan.stats
        pending_files = Counter(planned.per_folder_pending_files)
        pending_bytes = Counter(planned.per_folder_pending_bytes)
        failed_folders = {rel_top(problem.rel) for problem in prepared.scanned.problems}
        failed_folders.update(
            rel_top(issue.rel)
            for issue in prepared.plan.warnings
            if issue.code == FOREIGN_ITEM_KEPT
        )
        if summary is not None and summary.run_id is not None:
            for op, state, _ in index.op_states(summary.run_id):
                folder = rel_top(op.rel)
                if state is OpState.DONE and op.kind in _CONTENT_WRITES:
                    pending_files[folder] -= 1
                    pending_bytes[folder] -= op.size
                elif state is OpState.FAILED:
                    failed_folders.add(folder)
                    if op.src_rel is not None:
                        failed_folders.add(rel_top(op.src_rel))

        previous = index.folder_stats()
        can_mark_ok = summary is not None and summary.result in (RunResult.OK, RunResult.PARTIAL)
        stats = []
        for folder in deps.cfg.active_folders:
            stat = FolderStat(folder) if folder not in previous else previous[folder]
            if can_mark_ok and folder not in failed_folders:
                stat.last_ok = now
            stat.files_local = local_files[folder]
            stat.bytes_local = local_bytes[folder]
            stat.files_pending = pending_files[folder]
            stat.bytes_pending = pending_bytes[folder]
            stat.updated_at = now
            stats.append(stat)
        index.put_folder_stats(stats)

    def preview(self, *, stop: Event | None = None) -> Preview:
        """Return a fresh plan, scan problems, quota and brakes without executing.

        Report preflight failures as plan errors. Raise LockedError if another
        operation holds the lock, Stopped on cancellation, or propagate unexpected errors.
        """
        deps = self._deps
        with RunLock(deps.paths.lock_file):
            try:
                prepared = self._prepare(stop if stop is not None else Event())
            except _PreflightError as exc:
                plan = Plan([], PlanStats(), errors=[PlanIssue(exc.code, "", exc.detail)])
                return Preview(plan, [], None, evaluate_brakes(plan, deps.cfg.safety))
            self._folder_stats(prepared, deps.clock().isoformat())
            return Preview(
                prepared.plan,
                prepared.scanned.problems,
                prepared.about,
                prepared.brake,
            )

    def undo(self, run_id: int | None = None, *, stop: Event | None = None) -> UndoSummary:
        """Untrash a run's logged items and restore missing paths beneath the source root.

        Use the latest trash log by default. Refuse runs from an earlier mirror setup.
        Restore local content only when it matches a journaled backup of a local deletion.
        Raise LockedError on contention or Stopped on cancellation. Propagate
        source preflight or refresh failures.
        """
        deps = self._deps
        stop = stop if stop is not None else Event()
        with RunLock(deps.paths.lock_file):
            if run_id is None:
                run_id = deps.index.last_run_with_trash()
            boundary = deps.index.get_meta(META_UNDO_BOUNDARY)
            if run_id is not None and boundary is not None and run_id <= int(boundary):
                raise NimboError(code="cli.undo_other_mirror")
            summary = UndoSummary(run_id)
            records = [] if run_id is None else deps.index.trash_log(run_id)
            if not records:
                return summary
            _check_stop(stop)
            root_uuid = self._root_uuid(restoring=True)
            root = deps.cfg.source.root.resolve(strict=True)
            root_fd = os.open(root, _DIR_FLAGS)
            try:
                return self._undo_locked(records, summary, root_uuid, root_fd, stop)
            finally:
                os.close(root_fd)

    def _undo_locked(
        self,
        records: list[tuple[str, str, bool]],
        summary: UndoSummary,
        root_uuid: str,
        root_fd: int,
        stop: Event,
    ) -> UndoSummary:
        deps = self._deps
        assert summary.run_id is not None
        snapshots = deps.index.undo_snapshots(summary.run_id)
        target = deps.cfg.target.folder_id
        target_item = deps.drive.get(target)
        if target_item is None or target_item.trashed or not target_item.is_folder:
            raise NimboError(code="target_missing")
        restored = []
        for file_id, rel, _ in sorted(records, key=lambda row: (row[1], row[0])):
            _check_stop(stop)
            try:
                item = deps.drive.get(file_id)
                if item is None or item.id != file_id:
                    raise NimboError(code="remote_missing")
                self._check_restore_scope(item, root_uuid, stop)
                item = deps.drive.update_metadata(file_id, trashed=False)
                if item.id != file_id or item.trashed:
                    raise NimboError(code="unsafe_restore_target")
            except Stopped:
                raise
            except NimboError as exc:
                summary.failed.append((rel, exc.code))
            else:
                restored.append((rel, item))
                summary.untrashed += 1

        self._reset_target()
        self._indexer(stop).refresh()
        for rel, item in restored:
            self._restore_tree(
                root_fd, rel, item, root_uuid, summary, snapshots.get(item.id, {}), stop
            )
        return summary

    def _check_restore_scope(self, item: RemoteFile, root_uuid: str, stop: Event) -> None:
        deps = self._deps
        target = deps.cfg.target.folder_id
        seen = {item.id}
        if item.id == target or item.is_google_native:
            raise NimboError(code="unsafe_restore_target")
        while True:
            if item.owner_root not in (None, root_uuid):
                raise NimboError(code="foreign-owner")
            if len(item.parents) != 1 or item.parents[0] in seen:
                raise NimboError(code="unsafe_restore_target")
            parent_id = item.parents[0]
            if parent_id == target:
                return
            seen.add(parent_id)
            _check_stop(stop)
            parent = deps.drive.get(parent_id)
            if parent is None or parent.trashed or not parent.is_folder:
                raise NimboError(code="unsafe_restore_target")
            item = parent

    def _restore_tree(
        self,
        root_fd: int,
        rel: str,
        item: RemoteFile,
        root_uuid: str,
        summary: UndoSummary,
        snapshots: dict[str, TrashFile],
        stop: Event,
    ) -> None:
        deps = self._deps
        pending = [(rel, item)]
        seen: set[str] = set()
        folders = []

        def check_download_stop(_: int) -> None:
            _check_stop(stop)

        while pending:
            _check_stop(stop)
            rel, item = pending.pop()
            try:
                if item.id in seen or item.trashed:
                    raise NimboError(code="unsafe_restore_target")
                seen.add(item.id)
                snapshot = snapshots.get(item.id)
                if (
                    snapshot is None
                    or not snapshot.owned_by_mirror
                    or (not item.is_folder and snapshot.backup_md5 is None)
                ):
                    raise NimboError(code="not_backed_up")
                if snapshot.reason != "deleted-locally":
                    raise NimboError(code="not_deleted_locally")
                if item.owner_root not in (None, root_uuid):
                    raise NimboError(code="foreign-owner")
                if item.is_google_native or (rel, item.is_folder) != (
                    snapshot.rel,
                    snapshot.is_folder,
                ):
                    raise NimboError(code="unsafe_restore_target")
                if not item.is_folder and (item.size, item.md5) != (
                    snapshot.backup_size,
                    snapshot.backup_md5,
                ):
                    raise NimboError(code="changed_since_backup")
                with _restore_parent(root_fd, rel) as (parent_fd, name):
                    if item.is_folder:
                        folder_fd, created = _open_restore_dir(parent_fd, name)
                        os.close(folder_fd)
                        if created:
                            folders.append((rel, item.modified_ms))
                    else:
                        # Pin the directory used for exclusive download staging and publish.
                        dest = Path(f"/proc/self/fd/{parent_fd}") / name
                        try:
                            deps.downloader.download(item, dest, on_bytes=check_download_stop)
                        except FileExistsError:
                            if not os.path.lexists(dest):
                                raise
                            summary.skipped_existing.append(rel)
                        except Md5Mismatch as exc:
                            raise NimboError(code="changed_since_backup") from exc
                        else:
                            summary.restored_files += 1
                if item.is_folder:
                    children = sorted(
                        deps.drive.list_children([item.id]),
                        key=lambda child: (nfc(child.name), child.id),
                    )
                    for child in reversed(children):
                        name = nfc(child.name)
                        child_rel = rel_join(rel, name)
                        if "/" in name or name in ("", ".", "..") or "\x00" in name:
                            summary.failed.append((child_rel, "unsafe_restore_path"))
                        elif child.parents != (item.id,):
                            summary.failed.append((child_rel, "unsafe_restore_target"))
                        else:
                            pending.append((child_rel, child))
            except Stopped:
                raise
            except NimboError as exc:
                summary.failed.append((rel, exc.code))
            except (NotADirectoryError, IsADirectoryError):
                summary.failed.append((rel, "unsafe_restore_path"))
            except OSError:
                summary.failed.append((rel, "restore_io_error"))

        for rel, modified_ms in reversed(folders):
            if modified_ms is None:
                continue
            _check_stop(stop)
            try:
                with _restore_parent(root_fd, rel) as (parent_fd, name):
                    folder_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
                    try:
                        ns = modified_ms * 1_000_000
                        os.utime(folder_fd, ns=(ns, ns))
                    finally:
                        os.close(folder_fd)
            except (OSError, NimboError):
                summary.failed.append((rel, "restore_io_error"))

    def verify(self, *, repair: bool = False, stop: Event | None = None) -> ScanAuditReport:
        """Audit a full listing against freshly hashed local files.

        Release the audit lock before optional repair, which obtains its own
        lock and runs backup with deep comparison. Raise LockedError, Stopped
        or a source, Drive, or storage error if the audit cannot finish.
        """
        deps = self._deps
        stop = stop if stop is not None else Event()
        with RunLock(deps.paths.lock_file):
            _check_stop(stop)
            self._root_uuid(auditing=True)
            self._reset_target()
            self._indexer(stop).full_listing()
            report = self._audit(stop)
        if repair:
            report.repaired = self.run(trigger="verify", stop=stop)
        return report

    def _audit(self, stop: Event) -> ScanAuditReport:
        deps = self._deps
        cfg = deps.cfg
        matcher = cfg.matcher()
        result = scan(cfg.source.root, cfg.active_folders, matcher, should_stop=stop.is_set)
        if result.missing_folders:
            raise _PreflightError("folder_missing", result.missing_folders[0])
        protected = set(result.protected)

        def in_scope(rel: str, is_dir: bool) -> bool:
            active = rel_top(rel) in active_folders
            path = rel
            while path and path not in protected:
                path = rel_parent(path)
            return active and path not in protected and not matcher.excluded(rel, is_dir)

        active_folders = set(cfg.active_folders)
        tree = deps.index.load_tree(cfg.target.folder_id)
        remote: dict[str, list[RemoteFile]] = {}
        for rel, item in tree.walk():
            _check_stop(stop)
            if not item.is_google_native and in_scope(rel, item.is_folder):
                remote.setdefault(rel, []).append(item)
        local = {rel: entry for rel, entry in result.entries.items() if in_scope(rel, entry.is_dir)}
        hasher = LocalHasher(cfg.source.root, deps.index, should_stop=stop.is_set)
        report = ScanAuditReport(
            extra_remote=sorted(remote.keys() - local.keys()),
            duplicates=sorted(rel for rel, items in remote.items() if len(items) > 1),
            scan_problems=result.problems,
        )
        for rel, entry in sorted(local.items()):
            _check_stop(stop)
            digest = None if entry.is_dir else hasher.md5(entry, force=True)
            items = remote.get(rel, ())
            if not items:
                report.missing_remote.append(rel)
            elif not any(
                item.is_folder == entry.is_dir
                and (
                    entry.is_dir
                    or (digest is not None and (entry.size, digest) == (item.size, item.md5))
                )
                for item in items
            ):
                report.mismatched.append(rel)
        return report
