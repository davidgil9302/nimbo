"""Generate previous mirrors and independently mutate their local and Drive trees."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from hashlib import md5
from typing import TypeVar
from unicodedata import normalize

from hypothesis import strategies as st

from nimbo.core.filters import Matcher
from nimbo.core.model import (
    FOLDER_MIME,
    OWNER_PROPERTY,
    SHORTCUT_MIME,
    LocalEntry,
    RemoteFile,
    is_under,
    nfc,
    rel_depth,
    rel_join,
    rel_name,
    rel_parent,
)

ROOT = "test-root"
OWNER = "test-owner"
FOLDERS = ("Projects", "Photos")
MATCHER = Matcher(("*.skip", "cache/"))
RENAME_NAMES = ("year one", "café", "cafe\u0301", "moon🌙", ".v2", "tab\tline\n")
NAMES = (*RENAME_NAMES, "x", "notes.txt", "file name.txt", "空 白", "naïve.jpeg")
NS = 1_000_000


@dataclass(frozen=True)
class _Node:
    data: bytes | None
    ms: int
    raw_name: str

    @property
    def is_dir(self) -> bool:
        return self.data is None


@dataclass(frozen=True)
class Scenario:
    """A desired local tree, its bytes and the previous Drive snapshot after mutations."""

    local: dict[str, LocalEntry]
    content: dict[str, bytes]
    remote: dict[str, RemoteFile]
    protected: tuple[str, ...]

    def md5(self, rel: str) -> str:
        """Return the real MD5 of the generated local file at rel."""
        return _md5(self.content[rel])


def _md5(data: bytes) -> str:
    return md5(data, usedforsecurity=False).hexdigest()


_T = TypeVar("_T")


def _pick(items: list[_T], index: int) -> _T:
    return items[index % len(items)]


def _fresh(paths: Iterable[str], parent: str, raw_name: str) -> tuple[str, str]:
    rel = rel_join(parent, nfc(raw_name))
    suffix = 1
    original = raw_name
    while rel in paths:
        raw_name = f"{original}.{suffix}"
        rel = rel_join(parent, nfc(raw_name))
        suffix += 1
    return rel, raw_name


def _seed(rows: list[tuple[int, int, int]]) -> dict[str, _Node]:
    result = {folder: _Node(None, 10, folder) for folder in FOLDERS}
    result["Projects/Old folder"] = _Node(None, 11, "Old folder")
    result["Projects/Old folder/café"] = _Node(b"abc", 11, "cafe\u0301")
    result["Projects/Old folder/.photo 🌙"] = _Node(b"o", 12, ".photo 🌙")
    result["Photos/year one.txt"] = _Node(b"11", 0, "year one.txt")
    result["Photos/.empty"] = _Node(b"", 13, ".empty")
    for parent_selector, name_selector, material in rows:
        parents = [rel for rel, node in result.items() if node.is_dir and rel_depth(rel) < 4]
        parent = _pick(parents, parent_selector)
        rel, raw = _fresh(result, parent, _pick(list(NAMES), name_selector))
        size = material % 7
        data = None if material % 4 == 0 else bytes([material % 5]) * size
        result[rel] = _Node(data, material % 50, raw)
    return result


def _disk_rel(tree: dict[str, _Node], rel: str) -> str:
    parts = []
    while rel:
        parts.append(tree[rel].raw_name)
        rel = rel_parent(rel)
    return "/".join(reversed(parts))


def _local_snapshot(tree: dict[str, _Node]) -> tuple[dict[str, LocalEntry], dict[str, bytes]]:
    entries, content = {}, {}
    for rel, node in tree.items():
        disk = _disk_rel(tree, rel)
        data = node.data
        entries[rel] = LocalEntry(
            rel, data is None, len(data or b""), node.ms * NS, disk if disk != rel else None
        )
        if data is not None:
            content[rel] = data
    return entries, content


def _remote_snapshot(tree: dict[str, _Node]) -> tuple[dict[str, RemoteFile], dict[str, str]]:
    ids = {rel: f"old-{index}" for index, rel in enumerate(tree)}
    files = {}
    for rel, node in tree.items():
        file_id = ids[rel]
        files[file_id] = _remote_file(file_id, node, ids.get(rel_parent(rel), ROOT))
    return files, ids


def _remote_file(file_id: str, node: _Node, parent: str) -> RemoteFile:
    data = node.data
    return RemoteFile(
        file_id,
        nfc(node.raw_name),
        FOLDER_MIME if data is None else "application/octet-stream",
        (parent,),
        None if data is None else len(data),
        None if data is None else _md5(data),
        node.ms,
        {OWNER_PROPERTY: OWNER},
    )


_LOCAL_MUTATIONS = (
    "rename-file",
    "move-file",
    "rename-dir",
    "move-dir",
    "edit-size",
    "edit-same-size",
    "silent-edit",
    "touch",
    "delete",
    "add-file",
    "add-dir",
    "replace-type",
)
_REMOTE_MUTATIONS = (
    "extra-file",
    "extra-folder",
    "dupe-file",
    "dupe-folder",
    "unmanaged-copy",
    "unmanaged-dupe",
    "native",
    "excluded",
    "protected",
    "nfd",
    "outside",
    "invalid",
)


def _mutate_local(tree: dict[str, _Node], kind: str, index: int, extra: int) -> None:
    paths = sorted(tree)
    dirs = [rel for rel in paths if tree[rel].is_dir]
    parents = [rel for rel in dirs if rel_depth(rel) < 4]
    nested = [rel for rel in paths if rel_depth(rel) > 1]
    files = [rel for rel in nested if not tree[rel].is_dir]
    if kind in ("add-file", "add-dir"):
        rel, raw = _fresh(tree, _pick(parents, index), _pick(list(NAMES), extra))
        tree[rel] = _Node(None if kind == "add-dir" else bytes([index]), extra, raw)
    elif kind in ("delete", "replace-type") and nested:
        src = _pick(nested, index)
        old = tree[src]
        for rel in paths:
            if is_under(rel, src):
                del tree[rel]
        if kind == "replace-type":
            tree[src] = replace(old, data=b"new" if old.is_dir else None)
    elif kind in ("edit-size", "edit-same-size", "silent-edit", "touch") and files:
        rel = _pick(files, index)
        old = tree[rel]
        assert old.data is not None
        data = old.data
        if kind == "edit-size" or (kind == "edit-same-size" and not data):
            data += bytes([extra])
        elif kind != "touch" and data:
            data = bytes([data[0] ^ 1]) + data[1:]
        tree[rel] = replace(old, data=data, ms=old.ms + (kind != "silent-edit"))
    elif kind in ("rename-file", "move-file", "rename-dir", "move-dir"):
        candidates = files if kind.endswith("file") else [rel for rel in dirs if rel in nested]
        if not candidates:
            return
        src = _pick(candidates, index)
        subtree = [rel for rel in paths if is_under(rel, src)]
        extra_depth = max(rel_depth(rel) - rel_depth(src) for rel in subtree)
        parent = rel_parent(src)
        if kind.startswith("move"):
            destinations = [
                rel
                for rel in parents
                if not is_under(rel, src)
                and rel_depth(rel) + 1 + extra_depth <= 4
                and rel != parent
            ]
            if not destinations:
                return
            parent = _pick(destinations, extra)
        raw = tree[src].raw_name if kind.startswith("move") else _pick(list(RENAME_NAMES), extra)
        dest, raw = _fresh(tree, parent, raw)
        for rel in subtree:
            node = tree.pop(rel)
            tree[dest + rel[len(src) :]] = replace(node, raw_name=raw) if rel == src else node


def _mutate_remote(
    remote: dict[str, RemoteFile],
    ids: dict[str, str],
    previous: dict[str, _Node],
    local: dict[str, _Node],
    protected: set[str],
    serial: int,
    kind: str,
    index: int,
    *,
    parent_rel: str | None = None,
) -> None:
    fresh = f"extra-{serial}"
    if parent_rel is None:
        # Keep synthetic side trees within the generated depth bound.
        dirs = [rel for rel, node in previous.items() if node.is_dir and rel_depth(rel) <= 2]
        parent_rel = _pick(dirs, index)
    parent = ids[parent_rel]
    if kind in ("dupe-file", "dupe-folder", "unmanaged-copy", "unmanaged-dupe", "nfd"):
        candidates = []
        for rel, old in previous.items():
            current = local.get(rel)
            if kind == "dupe-folder":
                if old.is_dir and rel not in FOLDERS and old.ms > 0:
                    candidates.append(rel)
            elif kind == "nfd":
                if nfc(rel_name(rel)) != normalize("NFD", rel_name(rel)):
                    candidates.append(rel)
            elif not old.is_dir and (
                kind == "dupe-file" or (current is not None and old.data == current.data)
            ):
                candidates.append(rel)
        if not candidates:
            return
        rel = _pick(candidates, index)
        file_id = ids[rel]
        item = remote[file_id]
        if kind == "nfd":
            remote[file_id] = replace(item, name=normalize("NFD", item.name))
        elif kind == "unmanaged-copy":
            remote[file_id] = replace(item, properties={})
        elif kind != "unmanaged-dupe" or item.owner_root == OWNER:
            remote[fresh] = replace(
                item,
                id=fresh,
                modified_ms=0,
                properties={} if kind == "unmanaged-dupe" else {OWNER_PROPERTY: OWNER},
            )
    elif kind == "outside":
        remote[fresh] = _remote_file(fresh, _Node(None, 3, f"Outside {serial}"), ROOT)
        remote[f"{fresh}-item"] = _remote_file(f"{fresh}-item", _Node(b"x", 4, "保留"), fresh)
    elif kind == "native":
        remote[fresh] = RemoteFile(
            fresh,
            f"Online {serial}",
            SHORTCUT_MIME if index % 2 else "application/vnd.google-apps.document",
            (parent,),
        )
    elif kind in ("extra-folder", "protected", "excluded"):
        name = f"Remote folder {serial}" + (".skip" if kind == "excluded" else "")
        if kind == "protected":
            protected.add(rel_join(parent_rel, name))
        remote[fresh] = _remote_file(fresh, _Node(None, 4, name), parent)
        child = f"{fresh}-item"
        remote[child] = _remote_file(child, _Node(b"xyz", 5, "extra"), fresh)
    else:
        name = f"bad/{serial}" if kind == "invalid" else f"Extra {serial} 📎"
        remote[fresh] = _remote_file(fresh, _Node(b"extra", 5, name), parent)


@st.composite
def scenarios(draw: st.DrawFn, *, allow_unmanaged_duplicates: bool = True) -> Scenario:
    """Draw broad, shallow mirrors and a bounded sequence of local and remote changes."""
    indices = st.integers(min_value=0, max_value=50)
    previous = _seed(draw(st.lists(st.tuples(indices, indices, indices), max_size=18)))
    local = dict(previous)
    mutations = draw(
        st.lists(
            st.tuples(st.sampled_from(_LOCAL_MUTATIONS), indices, indices), min_size=1, max_size=7
        )
    )
    for kind, index, extra in mutations:
        _mutate_local(local, kind, index, extra)

    conflict = "Projects/Old folder"
    barrier = draw(st.one_of(st.none(), st.sampled_from(("protected", "excluded", "native"))))
    if barrier is not None:
        local = {rel: node for rel, node in local.items() if not is_under(rel, conflict)}
        local[conflict] = replace(previous[conflict], data=b"replacement")

    remote, ids = _remote_snapshot(previous)
    protected: set[str] = set()
    remote_kinds = (
        _REMOTE_MUTATIONS
        if allow_unmanaged_duplicates
        # Combining unmanaged-copy with dupe-file also yields a preserved unmanaged duplicate.
        else tuple(
            kind for kind in _REMOTE_MUTATIONS if kind not in ("unmanaged-copy", "unmanaged-dupe")
        )
    )
    mutations_remote = draw(
        st.lists(st.tuples(st.sampled_from(remote_kinds), indices), min_size=1, max_size=7)
    )
    for serial, (kind, index) in enumerate(mutations_remote):
        _mutate_remote(remote, ids, previous, local, protected, serial, kind, index)
    if barrier is not None:
        _mutate_remote(
            remote,
            ids,
            previous,
            local,
            protected,
            len(mutations_remote),
            barrier,
            0,
            parent_rel=conflict,
        )
    entries, content = _local_snapshot(local)
    return Scenario(entries, content, remote, tuple(sorted(protected)))
