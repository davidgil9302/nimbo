"""Build deterministic Drive operations from local and remote snapshots."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from nimbo.core.model import (
    INVALID_NAME_PREFIX,
    LocalEntry,
    Op,
    OpKind,
    Plan,
    PlanIssue,
    PlanStats,
    RemoteFile,
    drive_ms,
    rel_depth,
    rel_name,
    rel_parent,
    rel_top,
)
from nimbo.core.tree import RemoteTree


class MatcherLike(Protocol):
    """Exclusion policy for files and directories at relative paths."""

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """Return whether the item or a parent directory is excluded."""
        ...


@dataclass(frozen=True, slots=True)
class PlannerOptions:
    """Set the active top-level folders, owner, comparison mode and protected paths."""

    folders: tuple[str, ...]
    root_uuid: str
    deep: bool = False
    protected: tuple[str, ...] = ()


def _path_order(rel: str) -> tuple[int, str]:
    return rel_depth(rel), rel


def _invalid(rel: str) -> bool:
    return any(part.startswith(INVALID_NAME_PREFIX) for part in rel.split("/"))


def _under_protection(rel: str, protected: set[str]) -> bool:
    while rel and rel not in protected:
        rel = rel_parent(rel)
    return rel in protected


class _Remote:
    """Index the physical parent of each ID, including covered and duplicate items."""

    def __init__(
        self,
        tree: RemoteTree,
        local: Mapping[str, LocalEntry],
        matcher: MatcherLike,
        options: PlannerOptions,
        plan: Plan,
    ) -> None:
        self.items: dict[str, RemoteFile] = {}
        self.rels: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self.children: dict[str, set[str]] = defaultdict(set)
        self.eligible: set[str] = set()
        self.root = tree.root_id
        candidates = set(options.folders)
        protected = set(options.protected)
        for rel, item in tree.walk():
            if rel_top(rel) not in candidates:
                if rel_depth(rel) == 1:
                    plan.warnings.append(PlanIssue("outside-scope", rel))
                continue
            parent = next(p for p in item.parents if p == self.root or tree.get(p) is not None)
            self.items[item.id] = item
            self.rels[item.id] = rel
            self.parents[item.id] = parent
            self.children[parent].add(item.id)
            if item.owner_root not in (None, options.root_uuid):
                plan.errors.append(PlanIssue("foreign-owner", rel))
            if item.is_google_native:
                plan.warnings.append(PlanIssue("google-native-kept", rel))
            elif (
                (parent != self.root and parent not in self.eligible)
                or _under_protection(rel, protected)
                or matcher.excluded(rel, item.is_folder)
            ):
                if rel not in local or local[rel].is_dir != item.is_folder:
                    plan.warnings.append(PlanIssue("protected-kept", rel))
            else:
                self.eligible.add(item.id)
            if item.id not in self.eligible:
                entry = local.get(rel)
                if entry is not None and entry.is_dir != item.is_folder:
                    plan.warnings.append(PlanIssue("type-conflict-kept", rel))

    def descendants(self, file_id: str) -> Iterator[str]:
        pending = list(self.children.get(file_id, ()))
        while pending:
            child = pending.pop()
            yield child
            pending.extend(self.children.get(child, ()))

    def move(self, file_id: str, dest: str, parent: str | None, descendants: list[str]) -> Op:
        source = self.rels[file_id]
        old_parent = self.parents[file_id]
        self.children[old_parent].remove(file_id)
        if parent is not None:
            self.children[parent].add(file_id)
        else:
            # The new parent has no Drive ID until allocation. The moved subtree is
            # detached from its old ancestors for the purposes of trash accounting.
            parent = ""
        self.parents[file_id] = parent
        self.rels[file_id] = dest
        for child in descendants:
            self.rels[child] = dest + self.rels[child][len(source) :]
        return Op(
            OpKind.MOVE,
            dest,
            "renamed",
            file_id=file_id,
            parent_rel=rel_parent(dest),
            src_rel=source,
            src_parent_id=old_parent,
            name=rel_name(dest),
        )


def _choose(
    candidates: list[RemoteFile],
    local: LocalEntry,
    owner: str,
    md5_of: Callable[[LocalEntry], str | None],
) -> RemoteFile:
    if len(candidates) == 1:
        return candidates[0]
    keys = [(item.is_folder != local.is_dir, item.owner_root != owner) for item in candidates]
    best = min(keys)
    contenders = [item for item, key in zip(candidates, keys, strict=True) if key == best]
    md5 = None
    if (
        len(contenders) > 1
        and not local.is_dir
        and any(not item.is_folder and item.size == local.size and item.md5 for item in contenders)
    ):
        md5 = md5_of(local)
    return min(
        contenders,
        key=lambda item: (
            not (md5 is not None and item.md5 == md5 and item.size == local.size),
            -(item.modified_ms if item.modified_ms is not None else -1),
            item.id,
        ),
    )


def _pair(
    remote: _Remote,
    local: Mapping[str, LocalEntry],
    owner: str,
    md5_of: Callable[[LocalEntry], str | None],
    warnings: list[PlanIssue],
) -> tuple[dict[str, RemoteFile], dict[str, str]]:
    # Equal relative paths under different Drive folder IDs are separate branches.
    # Select each parent first, so a chosen child can never ride a trashed duplicate.
    candidates: dict[tuple[str, str], list[RemoteFile]] = defaultdict(list)
    for file_id in sorted(remote.eligible):
        candidates[remote.parents[file_id], remote.rels[file_id]].append(remote.items[file_id])
    pairs: dict[str, RemoteFile] = {}
    dirs = {"": remote.root}
    trash = {}
    for file_id in remote.items:
        if file_id in remote.eligible:
            item = remote.items[file_id]
            trash[file_id] = "deleted-locally" if item.owner_root == owner else "extraneous"
    for rel, entry in local.items():
        parent = dirs.get(rel_parent(rel))
        options = candidates.get((parent, rel)) if parent is not None else None
        if not options:
            continue
        keep = _choose(options, entry, owner, md5_of)
        pairs[rel] = keep
        if keep.is_folder == entry.is_dir:
            trash.pop(keep.id)
            if keep.is_folder:
                dirs[rel] = keep.id
        else:
            trash[keep.id] = "type-conflict"
        for dupe in options:
            if dupe.id == keep.id:
                continue
            if dupe.owner_root == owner:
                trash[dupe.id] = "duplicate"
            elif (
                dupe.is_folder
                and not entry.is_dir
                and any(child not in remote.eligible for child in remote.descendants(dupe.id))
            ):
                # This branch is already known to survive. Keep its eligible children
                # in the trash pool even after the mirrored file becomes the keeper.
                trash[dupe.id] = "type-conflict"
            else:
                warnings.append(PlanIssue("duplicate-unmanaged", rel))
                trash.pop(dupe.id)
                for child in remote.descendants(dupe.id):
                    trash.pop(child, None)
    return pairs, trash


def _destination_parent(rel: str, pairs: Mapping[str, RemoteFile], root: str) -> str | None:
    parent_rel = rel_parent(rel)
    if not parent_rel:
        return root
    parent = pairs.get(parent_rel)
    return parent.id if parent is not None and parent.is_folder else None


def _shape(rows: Iterable[tuple[str, bool, int | None]]) -> str | None:
    lines = []
    has_file = False
    for subrel, is_dir, size in rows:
        # Escape arbitrary path characters before framing each row with tabs/newlines.
        path = json.dumps(subrel, ensure_ascii=True)
        lines.append(f"{path}\tD" if is_dir else f"{path}\tF\t{size}")
        has_file |= not is_dir
    if not has_file:
        return None
    return hashlib.sha256("\n".join(sorted(lines)).encode("ascii")).hexdigest()


def _local_descendants(children: Mapping[str, list[str]], rel: str) -> Iterator[str]:
    pending = list(children.get(rel, ()))
    while pending:
        child = pending.pop()
        yield child
        pending.extend(children.get(child, ()))


def _remote_shape(remote: _Remote, file_id: str, trash: Mapping[str, str]) -> str | None:
    children = list(remote.descendants(file_id))
    if any(child not in trash or _invalid(remote.rels[child]) for child in children):
        return None
    prefix = len(remote.rels[file_id]) + 1
    return _shape(
        (remote.rels[child][prefix:], remote.items[child].is_folder, remote.items[child].size)
        for child in children
    )


def _folder_moves(
    remote: _Remote,
    local: Mapping[str, LocalEntry],
    pairs: dict[str, RemoteFile],
    trash: dict[str, str],
    owner: str,
) -> tuple[list[Op], set[str]]:
    missing_dirs = [rel for rel, entry in local.items() if entry.is_dir and rel not in pairs]
    if not missing_dirs:
        return [], set()

    sources = {
        file_id
        for file_id in trash
        if remote.items[file_id].is_folder
        and remote.items[file_id].owner_root == owner
        and remote.rels[file_id] not in local
        and not _invalid(remote.rels[file_id])
    }
    if not sources:
        return [], set()

    remote_groups: dict[str, set[str]] = defaultdict(set)
    remote_shapes: dict[str, str] = {}

    def refresh(file_id: str) -> None:
        old = remote_shapes.pop(file_id, None)
        if old is not None:
            remote_groups[old].remove(file_id)
        if file_id in sources:
            shape = _remote_shape(remote, file_id, trash)
            if shape is not None:
                remote_shapes[file_id] = shape
                remote_groups[shape].add(file_id)

    for file_id in sources:
        refresh(file_id)

    children: dict[str, list[str]] = defaultdict(list)
    for rel in local:
        children[rel_parent(rel)].append(rel)
    local_shapes = {}
    local_groups: dict[str, set[str]] = defaultdict(set)
    for rel in missing_dirs:
        shape = _shape(
            (child[len(rel) + 1 :], local[child].is_dir, local[child].size)
            for child in _local_descendants(children, rel)
        )
        if shape is not None:
            local_shapes[rel] = shape
            local_groups[shape].add(rel)

    moves = []
    relocated_files = set()
    for rel in missing_dirs:
        shape = local_shapes.get(rel)
        if shape is None or rel in pairs or len(local_groups[shape]) != 1:
            continue
        matches = remote_groups.get(shape)
        if matches is None or len(matches) != 1:
            continue
        (file_id,) = matches
        descendants = list(remote.descendants(file_id))
        parent_id = _destination_parent(rel, pairs, remote.root)
        move = remote.move(file_id, rel, parent_id, descendants)
        moves.append(move)
        for moved in (file_id, *descendants):
            dest = remote.rels[moved]
            item = remote.items[moved]
            pairs[dest] = item
            trash.pop(moved)
            if item.is_folder:
                sources.discard(moved)
                refresh(moved)
                if dest in local_shapes:
                    local_groups[local_shapes[dest]].remove(dest)
            else:
                relocated_files.add(dest)

        # Moving a nested source changes the remaining shape at each old ancestor.
        ancestor = move.src_parent_id
        while ancestor in remote.items:
            if ancestor in sources:
                refresh(ancestor)
            ancestor = remote.parents[ancestor]
    return moves, relocated_files


class _FileMatches:
    def __init__(self) -> None:
        self.files: deque[str] = deque()
        self.by_name: dict[str, deque[str]] = defaultdict(deque)
        self.by_parent: dict[str, deque[str]] = defaultdict(deque)

    def add(self, file_id: str, rel: str) -> None:
        self.files.append(file_id)
        self.by_name[rel_name(rel)].append(file_id)
        self.by_parent[rel_parent(rel)].append(file_id)

    def take(self, rel: str, trash: Mapping[str, str]) -> str | None:
        for pool in (self.by_name[rel_name(rel)], self.by_parent[rel_parent(rel)], self.files):
            while pool and pool[0] not in trash:
                pool.popleft()
            if pool:
                return pool.popleft()
        return None


def _file_moves(
    remote: _Remote,
    local: Mapping[str, LocalEntry],
    pairs: dict[str, RemoteFile],
    trash: dict[str, str],
    md5_of: Callable[[LocalEntry], str | None],
    owner: str,
) -> tuple[list[Op], list[Op]]:
    groups: dict[tuple[int, str], _FileMatches] = defaultdict(_FileMatches)
    sizes: Counter[int] = Counter()
    for candidate in sorted(trash, key=lambda item: (remote.rels[item], item)):
        item = remote.items[candidate]
        rel = remote.rels[candidate]
        if (
            item.is_folder
            or item.owner_root != owner
            or item.size is None
            or item.size <= 0
            or not item.md5
            or rel in local
            or _invalid(rel)
        ):
            continue
        groups[item.size, item.md5].add(candidate, rel)
        sizes[item.size] += 1

    moves = []
    touches = []
    for rel in sorted(local):
        entry = local[rel]
        if entry.is_dir or rel in pairs or not sizes[entry.size]:
            continue
        md5 = md5_of(entry)
        group = groups.get((entry.size, md5)) if md5 else None
        file_id = group.take(rel, trash) if group is not None else None
        if file_id is None:
            continue

        parent_id = _destination_parent(rel, pairs, remote.root)
        move = remote.move(file_id, rel, parent_id, [])
        move.md5 = md5
        move.mtime_ns = entry.mtime_ns
        moves.append(move)
        item = remote.items[file_id]
        # The executor interprets zero in a MOVE as "keep modifiedTime".
        if entry.mtime_ns == 0 and item.modified_ms != 0:
            touches.append(Op(OpKind.TOUCH, rel, "metadata", file_id=file_id, md5=md5))
        pairs[rel] = item
        trash.pop(file_id)
        sizes[entry.size] -= 1
    return moves, touches


def _trash_ops(
    remote: _Remote,
    trash: Mapping[str, str],
    local: Mapping[str, LocalEntry],
    warnings: list[PlanIssue],
) -> list[Op]:
    # Count the remaining files along physical parent links after all virtual moves.
    # Work bottom-up so one retained descendant disqualifies every trashable ancestor.
    covered: dict[str, int] = {}
    for file_id in reversed(sorted(remote.items, key=lambda item: _path_order(remote.rels[item]))):
        if file_id not in trash:
            continue
        item = remote.items[file_id]
        children = remote.children.get(file_id, ())
        if any(child not in covered for child in children):
            if item.is_folder:
                rel = remote.rels[file_id]
                entry = local.get(rel)
                code = (
                    "type-conflict-kept"
                    if entry is not None and not entry.is_dir
                    else ("folder-kept-protected")
                )
                warnings.append(PlanIssue(code, rel))
            continue
        covered[file_id] = sum(covered[child] for child in children) + int(not item.is_folder)

    ops = []
    for file_id in sorted(covered, key=lambda item: (remote.rels[item], item)):
        if remote.parents[file_id] not in covered:
            rel = remote.rels[file_id]
            reason = "invalid-name" if _invalid(rel) else trash[file_id]
            ops.append(
                Op(OpKind.TRASH, rel, reason, file_id=file_id, affected_files=covered[file_id])
            )
    return ops


def _content_op(
    entry: LocalEntry,
    item: RemoteFile,
    check_hash: bool,
    md5_of: Callable[[LocalEntry], str | None],
) -> Op | None:
    same_time = drive_ms(entry.mtime_ns) == item.modified_ms
    md5 = None
    if entry.size == item.size:
        if same_time and not check_hash and item.owner_root is not None:
            return None
        md5 = md5_of(entry)
        if md5 is not None and md5 == item.md5:
            if not same_time:
                return Op(
                    OpKind.TOUCH,
                    entry.rel,
                    "metadata",
                    file_id=item.id,
                    mtime_ns=entry.mtime_ns,
                    md5=md5,
                )
            if item.owner_root is None:
                return Op(
                    OpKind.ADOPT,
                    entry.rel,
                    "adopted",
                    file_id=item.id,
                    mtime_ns=entry.mtime_ns,
                    md5=md5,
                )
            return None
    return Op(
        OpKind.UPDATE,
        entry.rel,
        "changed",
        file_id=item.id,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        md5=md5,
        disk_rel=entry.disk,
    )


def _stats(ops: Iterable[Op], remote_files: dict[str, int]) -> PlanStats:
    counts: Counter[OpKind] = Counter()
    bytes_sent: Counter[OpKind] = Counter()
    trashed: Counter[str] = Counter()
    pending_files: Counter[str] = Counter()
    pending_bytes: Counter[str] = Counter()
    for op in ops:
        counts[op.kind] += 1
        top = rel_top(op.rel)
        if op.kind in (OpKind.UPLOAD, OpKind.UPDATE):
            bytes_sent[op.kind] += op.size
            pending_files[top] += 1
            pending_bytes[top] += op.size
        elif op.kind == OpKind.TRASH:
            trashed[top] += op.affected_files
    return PlanStats(
        mkdirs=counts[OpKind.MKDIR],
        uploads=counts[OpKind.UPLOAD],
        updates=counts[OpKind.UPDATE],
        upload_bytes=bytes_sent[OpKind.UPLOAD],
        update_bytes=bytes_sent[OpKind.UPDATE],
        touches=counts[OpKind.TOUCH],
        moves=counts[OpKind.MOVE],
        adopts=counts[OpKind.ADOPT],
        trashes=counts[OpKind.TRASH],
        trashed_files=sum(trashed.values()),
        per_folder_remote_files=remote_files,
        per_folder_trashed=dict(trashed),
        per_folder_pending_files=dict(pending_files),
        per_folder_pending_bytes=dict(pending_bytes),
    )


def make_plan(
    local: Mapping[str, LocalEntry],
    tree: RemoteTree,
    matcher: MatcherLike,
    hash_of: Callable[[LocalEntry], str | None],
    options: PlannerOptions,
) -> Plan:
    """Return ordered mirror operations without changing either input snapshot.

    Local keys are NFC relative paths. The callback must supply a digest of
    current bytes or return None if unavailable. Read it lazily and cache both
    results for this plan. A foreign owner or missing destination parent yields
    an error plan with no operations. Callback errors propagate to the caller.
    """
    folders = set(options.folders)
    protected = set(options.protected)
    entries = {
        rel: local[rel]
        for rel in sorted(local, key=_path_order)
        if rel_top(rel) in folders
        and not _under_protection(rel, protected)
        and not matcher.excluded(rel, local[rel].is_dir)
    }
    plan = Plan([], PlanStats(), folder_ids={"": tree.root_id})
    remote = _Remote(tree, entries, matcher, options, plan)
    if plan.errors:
        return plan

    hashes: dict[str, str | None] = {}

    def md5_of(entry: LocalEntry) -> str | None:
        if entry.rel not in hashes:
            hashes[entry.rel] = hash_of(entry)
        return hashes[entry.rel]

    counts = Counter(
        rel_top(rel)
        for file_id, rel in remote.rels.items()
        if file_id in remote.eligible and not remote.items[file_id].is_folder
    )
    pairs, trash = _pair(remote, entries, options.root_uuid, md5_of, plan.warnings)
    folder_moves, relocated = _folder_moves(remote, entries, pairs, trash, options.root_uuid)
    file_moves, contents = _file_moves(remote, entries, pairs, trash, md5_of, options.root_uuid)
    trashes = _trash_ops(remote, trash, entries, plan.warnings)
    moved_files = {op.rel for op in file_moves}
    mkdirs = []
    for rel, entry in entries.items():
        item = pairs.get(rel)
        if item is not None and item.is_folder == entry.is_dir:
            if entry.is_dir:
                plan.folder_ids[rel] = item.id
            elif rel not in moved_files:
                op = _content_op(entry, item, options.deep or rel in relocated, md5_of)
                if op is not None:
                    contents.append(op)
            continue
        if entry.is_dir:
            mkdirs.append(
                Op(OpKind.MKDIR, rel, "new", parent_rel=rel_parent(rel), name=rel_name(rel))
            )
        else:
            contents.append(
                Op(
                    OpKind.UPLOAD,
                    rel,
                    "new",
                    parent_rel=rel_parent(rel),
                    name=rel_name(rel),
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    md5=hashes.get(rel),
                    disk_rel=entry.disk,
                )
            )

    resolved = set(plan.folder_ids)
    ops = [*mkdirs, *folder_moves, *file_moves, *sorted(contents, key=lambda op: op.rel), *trashes]
    for op in ops:
        if op.parent_rel is not None and op.parent_rel not in resolved:
            plan.errors.append(PlanIssue("plan-inconsistent", op.rel))
        if op.kind is OpKind.MKDIR:
            resolved.add(op.rel)

    new_dirs = {op.rel for op in mkdirs}
    for file_id, rel in sorted(remote.rels.items(), key=lambda item: (item[1], item[0])):
        if file_id in remote.eligible and remote.items[file_id].is_folder and rel not in new_dirs:
            plan.folder_ids.setdefault(rel, file_id)
    plan.warnings.sort(key=lambda issue: (issue.rel, issue.code))
    plan.stats = _stats(ops if not plan.errors else (), dict(counts))
    if not plan.errors:
        plan.ops = ops
    return plan
