"""Build and refresh the transactional index of a Drive target subtree."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from nimbo.core.drive import LIST_GROUP_MAX, DriveClient
from nimbo.core.errors import BadRequest, DriveError, NimboError, NotFound, Stopped
from nimbo.core.index import (
    META_CHANGES_PROBE,
    META_CHANGES_RELIABLE,
    META_CHANGES_TOKEN,
    META_FORCE_FULL,
    META_LAST_FULL,
    Index,
)
from nimbo.core.model import RemoteFile
from nimbo.core.tree import RemoteTree

_PROBE_LIMIT = 20
_PROBE_GRACE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Report the refresh mode, stored rows (including the root) and applied feed events."""

    mode: Literal["full", "changes"]
    items: int
    changes: int


@dataclass(slots=True)
class _Progress:
    callback: Callable[[int], None] | None
    seen: int = 0

    def advance(self, count: int) -> None:
        self.seen += count
        if self.callback is not None:
            self.callback(self.seen)


@dataclass(slots=True)
class _ChangeBatch:
    token: str
    files: dict[str, RemoteFile | None] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)
    unavailable: set[str] = field(default_factory=set)


class RemoteIndexer:
    """Refresh a mirror under its canonical Drive folder ID.

    The caller serializes writers to the mirror, including refreshes and local
    operation bookkeeping. Drive requests for separate listing groups may overlap.
    """

    def __init__(
        self,
        drive: DriveClient,
        index: Index,
        root_id: str,
        *,
        clock: Callable[[], datetime],
        full_every: timedelta = timedelta(days=7),
        workers: int = 4,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        """Configure the clock, full-list interval, cancellation and progress callbacks.

        The callbacks run on the calling thread. Raise ValueError for fewer than
        one listing worker.
        """
        if workers < 1:
            raise ValueError("At least one listing worker is required")
        self._drive = drive
        self._index = index
        self._root_id = root_id
        self._clock = clock
        self._full_every = full_every
        self._workers = workers
        self._should_stop = should_stop
        self._on_progress = on_progress

    def refresh(self, *, force_full: bool = False) -> RefreshResult:
        """Refresh according to policy and report which mode committed the index.

        Raise Stopped on cancellation, NimboError for an unavailable target, or
        propagate Drive and storage errors that prevent refresh.
        """
        progress = _Progress(self._on_progress)
        token = self._index.get_meta(META_CHANGES_TOKEN)
        if (
            force_full
            or not token
            or self._index.get_meta(META_FORCE_FULL) is not None
            or self._index.get_meta(META_CHANGES_RELIABLE) == "0"
            or self._full_due()
        ):
            return self._full_listing(progress)
        return self._apply_changes(token, progress)

    def full_listing(self) -> int:
        """Replace the mirror and cursor together and return the stored row count.

        Raise Stopped on cancellation or NimboError with code target_missing if
        the target is unavailable. Propagate other Drive and storage errors.
        """
        return self._full_listing(_Progress(self._on_progress)).items

    def apply_changes(self) -> int:
        """Apply feed events affecting the mirror and return their count.

        Return zero when a full listing is needed. Raise Stopped on cancellation,
        NimboError if the target goes away, or propagate Drive and storage errors.
        """
        progress = _Progress(self._on_progress)
        token = self._index.get_meta(META_CHANGES_TOKEN)
        if not token or self._index.get_meta(META_CHANGES_RELIABLE) == "0":
            return self._full_listing(progress).changes
        return self._apply_changes(token, progress).changes

    def record_probe(self, created_ids: Sequence[str]) -> None:
        """Store up to 20 recently created IDs for the next feed-backed refresh.

        An empty sequence leaves a pending probe available for retry. Propagate
        storage errors if the metadata cannot be written.
        """
        if created_ids:
            probe = {
                "ids": list(created_ids[:_PROBE_LIMIT]),
                "recorded_at": self._clock().isoformat(),
            }
            self._index.set_meta(META_CHANGES_PROBE, json.dumps(probe))

    def _check_stop(self) -> None:
        if self._should_stop is not None and self._should_stop():
            raise Stopped

    def _full_due(self) -> bool:
        previous = self._index.get_meta(META_LAST_FULL)
        if not previous:
            return True
        try:
            age = self._clock() - datetime.fromisoformat(previous)
        except (TypeError, ValueError):
            return True
        return age >= self._full_every

    def _require_target(self, item: RemoteFile | None) -> RemoteFile:
        if item is None or item.trashed or not item.is_folder or item.id != self._root_id:
            raise NimboError("Cannot resolve a live Drive target folder", code="target_missing")
        return item

    def _reachable(self, files: dict[str, RemoteFile]) -> dict[str, RemoteFile]:
        tree = RemoteTree(self._root_id, files.values())
        reachable = {item.id: item for _, item in tree.walk()}
        reachable[self._root_id] = files[self._root_id]
        return reachable

    def _full_listing(
        self,
        progress: _Progress,
        *,
        reliability: Literal["0", "1"] | None = None,
        keep_probe: bool = False,
    ) -> RefreshResult:
        self._check_stop()
        meta: dict[str, str | None] = {}
        if not keep_probe:
            meta[META_CHANGES_TOKEN] = self._drive.start_page_token()
            # The new cursor deliberately skips the feed window covered by this probe.
            meta[META_CHANGES_PROBE] = None
        self._check_stop()
        root = self._require_target(self._drive.get(self._root_id))
        progress.advance(1)
        files = self._list_subtrees([root.id], progress)
        files[root.id] = root
        reachable = self._reachable(files)
        meta[META_LAST_FULL] = self._clock().isoformat()
        meta[META_FORCE_FULL] = None
        if reliability is not None:
            meta[META_CHANGES_RELIABLE] = reliability
        self._check_stop()
        self._index.replace_remote(reachable.values(), meta=meta)
        return RefreshResult("full", len(reachable), 0)

    def _list_subtrees(self, folders: list[str], progress: _Progress) -> dict[str, RemoteFile]:
        found: dict[str, RemoteFile] = {}
        scheduled = set(folders)
        width = self._workers * LIST_GROUP_MAX
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            while folders:
                next_level: list[str] = []
                for offset in range(0, len(folders), width):
                    self._check_stop()
                    window = folders[offset : offset + width]
                    pending = [
                        pool.submit(self._list_group, window[i : i + LIST_GROUP_MAX])
                        for i in range(0, len(window), LIST_GROUP_MAX)
                    ]
                    for future in as_completed(pending):
                        self._check_stop()
                        group = future.result()
                        for item in group:
                            if item.trashed or item.id == self._root_id:
                                continue
                            found[item.id] = item
                            if item.is_folder and item.id not in scheduled:
                                scheduled.add(item.id)
                                next_level.append(item.id)
                        progress.advance(len(group))
                folders = next_level
        return found

    def _list_group(self, folders: list[str]) -> list[RemoteFile]:
        return list(self._drive.list_children(folders))

    def _read_changes(self, token: str, progress: _Progress) -> _ChangeBatch | None:
        batch = _ChangeBatch(token)
        seen_tokens = {token}
        while True:
            self._check_stop()
            try:
                page = self._drive.changes(token)
            except (BadRequest, NotFound):
                return None
            except DriveError as error:
                if error.status == 410:
                    return None
                raise
            for change in page.changes:
                item = change.file
                if not change.removed and (item is None or item.id != change.file_id):
                    raise DriveError("Invalid file metadata in a Drive change")
                if change.removed or (item is not None and item.trashed):
                    batch.unavailable.add(change.file_id)
                    item = None
                batch.files[change.file_id] = item
                batch.counts[change.file_id] += 1
            progress.advance(len(page.changes))
            if not page.next_page_token:
                if not page.new_start_page_token:
                    raise DriveError("Drive changes ended without a new start page token")
                batch.token = page.new_start_page_token
                return batch
            token = page.next_page_token
            if token in seen_tokens:
                raise DriveError("Drive changes repeated a page token")
            seen_tokens.add(token)

    def _load_probe(self) -> tuple[set[str], timedelta | None] | None:
        raw = self._index.get_meta(META_CHANGES_PROBE)
        if raw is None:
            return set(), None
        age = None
        try:
            ids: object = json.loads(raw)
            if isinstance(ids, dict):
                timestamp = ids.get("recorded_at")
                if not isinstance(timestamp, str):
                    return None
                age = self._clock() - datetime.fromisoformat(timestamp)
                if age < timedelta(0):
                    return None
                ids = ids.get("ids")
        except (TypeError, ValueError):
            return None
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            return None
        return set(ids), age

    def _apply_changes(self, token: str, progress: _Progress) -> RefreshResult:
        self._check_stop()
        tree = self._index.load_tree(self._root_id)
        root = tree.get(self._root_id)
        if root is None or self._index.remote_count() != len(tree) + 1:
            # Reconciliation can leave rows outside the reachable mirror.
            return self._full_listing(progress)
        before = {item.id: item for _, item in tree.walk()}
        before[root.id] = root
        batch = self._read_changes(token, progress)
        self._check_stop()
        if batch is None:
            return self._full_listing(progress)

        self._require_target(batch.files.get(root.id, root))
        meta: dict[str, str | None] = {META_CHANGES_TOKEN: batch.token}
        probe = self._load_probe()
        if probe is None:
            return self._full_listing(progress)
        probes, age = probe
        if probes:
            if probes.isdisjoint(batch.files):
                if age is not None and age < _PROBE_GRACE:
                    # Reuse the same feed window so a delayed event can still satisfy the probe.
                    return self._full_listing(progress, keep_probe=True)
                return self._full_listing(progress, reliability="0")
            meta.update({META_CHANGES_RELIABLE: "1", META_CHANGES_PROBE: None})

        after = self._changed_snapshot(before, batch, progress)
        removed = before.keys() - after.keys()
        if removed and any(item.parents and item.parents[0] in removed for item in after.values()):
            # The target is reachable by ID even if a folder that left the
            # mirror became its parent. SQLite would follow that edge on delete.
            return self._full_listing(progress, reliability="1" if probes else None)
        delete_subtrees = [
            file_id
            for file_id in removed
            if not before[file_id].parents or before[file_id].parents[0] not in removed
        ]
        upserts = [item for file_id, item in after.items() if before.get(file_id) != item]
        self._check_stop()
        self._index.apply_remote_delta(upserts, delete_subtrees, meta=meta)
        changes = sum(
            count
            for file_id, count in batch.counts.items()
            if file_id in before or file_id in after
        )
        return RefreshResult("changes", len(after), changes)

    def _changed_snapshot(
        self, before: dict[str, RemoteFile], batch: _ChangeBatch, progress: _Progress
    ) -> dict[str, RemoteFile]:
        candidates = before.copy()
        for file_id, item in batch.files.items():
            if item is None:
                candidates.pop(file_id, None)
            else:
                candidates[file_id] = item

        listed = {self._root_id}
        while True:
            # Membership follows the final parent links, including links of
            # children whose change was delivered before their parent's change.
            reachable = self._reachable(candidates)
            folders = [
                item.id
                for item in reachable.values()
                if item.is_folder
                and item.id not in listed
                and (item.id not in before or item.id in batch.unavailable)
            ]
            if not folders:
                return reachable
            found = self._list_subtrees(folders, progress)
            listed.update(folders)
            listed.update(item.id for item in found.values() if item.is_folder)
            for file_id, item in found.items():
                if file_id not in batch.files:
                    candidates[file_id] = item
