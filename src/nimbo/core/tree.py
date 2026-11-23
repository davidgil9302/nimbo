"""Deterministic paths and traversal for a snapshot of the Drive mirror."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from nimbo.core.model import INVALID_NAME_PREFIX, RemoteFile, nfc, rel_join


def _rel_component(name: str) -> str:
    name = nfc(name)
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        # Keep an invalid name inside one rel component and escape literal escape sequences.
        return INVALID_NAME_PREFIX + nfc(name.replace("%", "%25").replace("/", "%2F"))
    return name


class RemoteTree:
    """Index non-trashed files by ID and by their paths from a Drive root."""

    root_id: str

    def __init__(self, root_id: str, files: Iterable[RemoteFile]) -> None:
        """Build a tree rooted at ``root_id``, even if the root row is omitted."""
        self.root_id = root_id
        self._files = {item.id: item for item in files if not item.trashed}
        self._children: dict[str, list[RemoteFile]] = {}
        for item in self._files.values():
            if item.id == root_id:
                continue
            for parent_id in item.parents:
                if parent_id == root_id or parent_id in self._files:
                    self._children.setdefault(parent_id, []).append(item)
                    break

        for children in self._children.values():
            children.sort(key=lambda item: (nfc(item.name), item.id))

        self._rels = {root_id: ""}
        for parent_id, item in self._walk_children(root_id):
            self._rels[item.id] = rel_join(self._rels[parent_id], _rel_component(item.name))

    def get(self, file_id: str) -> RemoteFile | None:
        """Return a known non-trashed file by ID, including unreachable files."""
        return self._files.get(file_id)

    def children(self, file_id: str) -> list[RemoteFile]:
        """Return direct children sorted by NFC name and then ID."""
        return list(self._children.get(file_id, ()))

    def rel_of(self, file_id: str) -> str | None:
        """Return the path from the root (``""`` for root), or ``None`` if unreachable."""
        return self._rels.get(file_id)

    def walk(self) -> Iterator[tuple[str, RemoteFile]]:
        """Yield reachable non-root entries in deterministic, parent-first order."""
        for file_id, rel in self._rels.items():
            if file_id != self.root_id:
                yield rel, self._files[file_id]

    def by_rel(self) -> dict[str, list[RemoteFile]]:
        """Group reachable non-root entries by rel with each group ordered by ID."""
        groups: dict[str, list[RemoteFile]] = {}
        for rel, item in self.walk():
            groups.setdefault(rel, []).append(item)
        for group in groups.values():
            group.sort(key=lambda item: item.id)
        return groups

    def descendants(self, file_id: str) -> Iterator[RemoteFile]:
        """Yield descendants in parent-first order, visiting each ID at most once."""
        for _, item in self._walk_children(file_id):
            yield item

    def count_files(self, file_id: str) -> int:
        """Count non-folder descendants, or return one when the ID names a file."""
        item = self.get(file_id)
        if item is not None and not item.is_folder:
            return 1
        return sum(not child.is_folder for child in self.descendants(file_id))

    def __contains__(self, file_id: object) -> bool:
        """Return whether the ID is reachable from the root, including the root ID."""
        return isinstance(file_id, str) and file_id in self._rels

    def __len__(self) -> int:
        """Return the number of reachable entries below the root."""
        return len(self._rels) - 1

    def _walk_children(self, file_id: str) -> Iterator[tuple[str, RemoteFile]]:
        visited = {file_id}
        stack = [(file_id, iter(self._children.get(file_id, ())))]
        while stack:
            parent_id, children = stack[-1]
            item = next(children, None)
            if item is None:
                stack.pop()
            elif item.id not in visited:
                visited.add(item.id)
                yield parent_id, item
                stack.append((item.id, iter(self._children.get(item.id, ()))))
