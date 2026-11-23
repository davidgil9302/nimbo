"""Apply planned writes to a single-parent Drive snapshot in memory."""

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import replace
from itertools import count

from nimbo.core.model import (
    FOLDER_MIME,
    OWNER_PROPERTY,
    LocalEntry,
    OpKind,
    Plan,
    RemoteFile,
    drive_ms,
    nfc,
    rel_top,
)
from nimbo.core.planner import MatcherLike


def apply_plan(
    files: dict[str, RemoteFile],
    plan: Plan,
    local: Mapping[str, LocalEntry],
    md5_of: Callable[[str], str],
    root_id: str,
    root_uuid: str,
) -> dict[str, RemoteFile]:
    """Return a copy with the plan applied in order, asserting valid Drive parents and IDs."""
    result = dict(files)
    folders = dict(plan.folder_ids)
    children: dict[str, set[str]] = defaultdict(set)
    for item in result.values():
        if item.parents:
            (parent,) = item.parents
            children[parent].add(item.id)
    serial = count(1)

    def path(file_id: str) -> str:
        parts = []
        visited = set()
        while file_id != root_id:
            assert file_id not in visited
            visited.add(file_id)
            item = result[file_id]
            assert item.is_folder and not item.trashed
            parts.append(nfc(item.name))
            (file_id,) = item.parents
        return "/".join(reversed(parts))

    for op in plan.ops:
        file_id = op.file_id
        parent = None
        if op.kind in (OpKind.MKDIR, OpKind.UPLOAD, OpKind.MOVE):
            assert op.parent_rel is not None and op.name is not None
            parent = folders[op.parent_rel]
            assert path(parent) == op.parent_rel
            assert op.rel == f"{op.parent_rel}/{op.name}".lstrip("/")
            assert op.parent_id is None or op.parent_id == parent

        if op.kind in (OpKind.MKDIR, OpKind.UPLOAD):
            assert parent is not None and op.name is not None
            if file_id is None:
                file_id = f"new-{next(serial)}"
                while file_id in result:
                    file_id = f"new-{next(serial)}"
            assert file_id not in result and file_id != root_id
            is_dir = op.kind is OpKind.MKDIR
            digest = None if is_dir else md5_of(op.rel)
            assert op.md5 is None or op.md5 == digest
            result[file_id] = RemoteFile(
                file_id,
                op.name,
                FOLDER_MIME if is_dir else "application/octet-stream",
                (parent,),
                None if is_dir else local[op.rel].size,
                digest,
                drive_ms(op.mtime_ns),
                {OWNER_PROPERTY: root_uuid},
            )
            children[parent].add(file_id)
            if is_dir:
                folders[op.rel] = file_id
            continue

        assert file_id is not None and file_id != root_id
        item = result[file_id]
        props = {**item.properties, OWNER_PROPERTY: root_uuid}
        if op.kind is OpKind.MOVE:
            assert parent is not None and op.name is not None
            (src_parent,) = item.parents
            assert op.src_parent_id == src_parent
            children[src_parent].remove(file_id)
            children[parent].add(file_id)
            result[file_id] = replace(
                item,
                name=op.name,
                parents=(parent,),
                modified_ms=drive_ms(op.mtime_ns) if op.mtime_ns else item.modified_ms,
                properties=props,
            )
        elif op.kind is OpKind.UPDATE:
            digest = md5_of(op.rel)
            assert op.md5 is None or op.md5 == digest
            result[file_id] = replace(
                item,
                size=local[op.rel].size,
                md5=digest,
                modified_ms=drive_ms(op.mtime_ns),
                properties=props,
            )
        elif op.kind in (OpKind.TOUCH, OpKind.ADOPT):
            result[file_id] = replace(item, modified_ms=drive_ms(op.mtime_ns), properties=props)
        else:
            assert op.kind is OpKind.TRASH
            (parent,) = item.parents
            children[parent].remove(file_id)
            pending = [file_id]
            removed_files = 0
            while pending:
                current = pending.pop()
                pending.extend(children.pop(current, ()))
                removed_files += not result.pop(current).is_folder
            assert removed_files == op.affected_files
    return result


def expected_view(
    local: Mapping[str, LocalEntry], matcher: MatcherLike, folders: tuple[str, ...]
) -> dict[str, LocalEntry]:
    """Return the local entries that should appear within the filtered mirror."""
    return {
        rel: entry
        for rel, entry in local.items()
        if rel_top(rel) in folders and not matcher.excluded(rel, entry.is_dir)
    }
