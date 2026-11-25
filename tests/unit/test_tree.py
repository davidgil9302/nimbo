"""Remote tree paths, reachability, and traversal."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.model import FOLDER_MIME, INVALID_NAME_PREFIX, RemoteFile, nfc
from nimbo.core.tree import RemoteTree


def _item(
    file_id: str, name: str, *parents: str, folder: bool = False, trashed: bool = False
) -> RemoteFile:
    return RemoteFile(
        file_id, name, FOLDER_MIME if folder else "text/plain", parents, trashed=trashed
    )


def test_rel_paths_nested() -> None:
    root = _item("root", "unused / name", folder=True)
    folder = _item("folder", "Photos", "unknown", "root", folder=True)
    leaf = _item("leaf", "image.jpg", "folder", "root")
    tree = RemoteTree("root", [leaf, folder, root])

    assert tree.root_id == "root"
    assert tree.get("root") == root
    assert tree.rel_of("root") == ""
    assert tree.rel_of("folder") == "Photos"
    assert tree.rel_of("leaf") == "Photos/image.jpg"
    assert tree.children("root") == [folder]
    assert list(tree.walk()) == [("Photos", folder), ("Photos/image.jpg", leaf)]
    assert "root" in tree
    assert len(tree) == 2


def test_trashed_items_absent() -> None:
    parent = _item("gone", "Gone", "root", folder=True, trashed=True)
    orphan = _item("orphan", "Orphan", "gone")
    fallback = _item("fallback", "Kept", "gone", "root")
    tree = RemoteTree("root", [parent, orphan, fallback])

    assert tree.get("gone") is None
    assert tree.rel_of("gone") is None
    assert tree.children("gone") == []
    assert "gone" not in tree
    assert "orphan" not in tree
    assert list(tree.walk()) == [("Kept", fallback)]


def test_unreachable_items_ignored() -> None:
    outside = _item("outside", "Outside", "missing", folder=True)
    leaf = _item("leaf", "Leaf", "outside", "root")
    tree = RemoteTree("root", [outside, leaf])

    assert tree.get("outside") == outside
    assert tree.get("unknown") is None
    assert tree.children("outside") == [leaf]
    assert list(tree.descendants("outside")) == [leaf]
    assert tree.rel_of("leaf") is None
    assert "leaf" not in tree
    assert [] not in tree
    assert list(tree.walk()) == []
    assert tree.by_rel() == {}
    assert len(tree) == 0


def test_duplicates_grouped_by_rel() -> None:
    folder_a = _item("a", "Photos", "root", folder=True)
    folder_b = _item("b", "Photos", "root", folder=True)
    leaf_a = _item("aa", "image.jpg", "b")
    leaf_b = _item("bb", "image.jpg", "a")
    tree = RemoteTree("root", [folder_b, leaf_b, leaf_a, folder_a])

    assert tree.by_rel() == {"Photos": [folder_a, folder_b], "Photos/image.jpg": [leaf_a, leaf_b]}


def test_invalid_names_get_prefix_and_never_collide() -> None:
    invalid = [
        _item(str(i), name, "root", folder=True)
        for i, name in enumerate(("", ".", "..", "a/", "a/leaf", "a%2F\x00", "a/\x00", "a/\u0307"))
    ]
    leaf = _item("leaf", "leaf", "3")
    valid = _item("valid", "a%2F", "root")
    tree = RemoteTree("root", [*invalid, leaf, valid])

    assert len(tree.by_rel()) == len(tree)
    for item in invalid:
        rel = tree.rel_of(item.id)
        assert rel is not None
        assert rel.startswith(INVALID_NAME_PREFIX)
        assert "/" not in rel
        assert rel == nfc(rel)
    assert tree.rel_of("0") == INVALID_NAME_PREFIX
    assert tree.rel_of("1") == INVALID_NAME_PREFIX + "."
    assert tree.rel_of("2") == INVALID_NAME_PREFIX + ".."
    assert tree.rel_of("leaf") == f"{tree.rel_of('3')}/leaf"
    assert tree.rel_of("valid") == "a%2F"


def test_cycle_is_cut() -> None:
    root = _item("root", "Root", "child", folder=True)
    child = _item("child", "Child", "root", folder=True)
    left = _item("left", "Left", "right", folder=True)
    right = _item("right", "Right", "left", folder=True)
    self_parent = _item("self", "Self", "self", folder=True)
    tree = RemoteTree("root", [left, child, right, root, self_parent])

    assert list(tree.walk()) == [("Child", child)]
    assert list(tree.descendants("root")) == [child]
    assert list(tree.descendants("left")) == [right]
    assert list(tree.descendants("self")) == []
    assert tree.rel_of("left") is None


def test_walk_parents_first_and_deterministic() -> None:
    upper = _item("z", "A", "root", folder=True)
    inside = _item("inside", "item", "z")
    lower = _item("a", "a", "root")
    accent_a = _item("accent-a", "e\u0301", "root")
    accent_b = _item("accent-b", "é", "root")
    files = [accent_b, lower, inside, accent_a, upper]
    expected = [("A", upper), ("A/item", inside), ("a", lower), ("é", accent_a), ("é", accent_b)]

    for order in (files, list(reversed(files)), sorted(files, key=lambda item: item.id)):
        tree = RemoteTree("root", iter(order))
        assert list(tree.walk()) == expected
        assert tree.children("root") == [upper, lower, accent_a, accent_b]


def test_count_files_and_descendants() -> None:
    folder = _item("folder", "Folder", "root", folder=True)
    inner = _item("inner", "Inner", "folder", folder=True)
    leaf = _item("leaf", "Leaf", "inner")
    sibling = _item("sibling", "Sibling", "root")
    empty = _item("empty", "Empty", "root", folder=True)
    tree = RemoteTree("root", [folder, leaf, sibling, empty, inner])

    assert list(tree.descendants("folder")) == [inner, leaf]
    assert list(tree.descendants("unknown")) == []
    assert tree.count_files("root") == 2
    assert tree.count_files("folder") == 1
    assert tree.count_files("leaf") == 1
    assert tree.count_files("empty") == 0
    assert tree.count_files("unknown") == 0


@given(st.text(), st.text())
def test_nfc_normalized_rel(folder_name: str, leaf_name: str) -> None:
    folder = _item("folder", "e\u0301" + folder_name, "root", folder=True)
    leaf = _item("leaf", leaf_name, "folder")
    tree = RemoteTree("root", [folder, leaf])
    folder_rel = tree.rel_of("folder")
    leaf_rel = tree.rel_of("leaf")

    assert folder_rel is not None and _is_rel_component(folder_rel)
    assert leaf_rel is not None
    assert leaf_rel.startswith(folder_rel + "/")
    assert _is_rel_component(leaf_rel.removeprefix(folder_rel + "/"))
    assert leaf_rel == nfc(leaf_rel)
    if "/" not in folder.name and "\x00" not in folder.name:
        assert folder_rel == nfc(folder.name)
    if leaf.name not in ("", ".", "..") and "/" not in leaf.name and "\x00" not in leaf.name:
        assert leaf_rel == f"{folder_rel}/{nfc(leaf.name)}"


def _is_rel_component(component: str) -> bool:
    return component not in ("", ".", "..") and "/" not in component and component == nfc(component)


def test_deep_tree_uses_iterative_traversal() -> None:
    tree = RemoteTree(
        "0", (_item(str(i), "x", str(i - 1), folder=i < 1500) for i in range(1, 1501))
    )

    assert len(list(tree.walk())) == 1500
    assert tree.rel_of("1500") == "/".join(["x"] * 1500)
    assert tree.count_files("0") == 1
