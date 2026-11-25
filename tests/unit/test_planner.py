"""Focused examples for planning, deletion accounting, and lazy hashing."""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace

import pytest

from nimbo.core.filters import Matcher
from nimbo.core.model import (
    FOLDER_MIME,
    OWNER_PROPERTY,
    LocalEntry,
    OpKind,
    Plan,
    PlanIssue,
    RemoteFile,
    drive_ms,
)
from nimbo.core.planner import PlannerOptions, make_plan
from nimbo.core.tree import RemoteTree
from tests.property.sim import apply_plan

ROOT = "root"
OWNER = "owner"
FOLDER = "Projects"
NS = 1_000_000


def _entry(path: str, *, size: int = 3, ms: int = 10, disk: str | None = None) -> LocalEntry:
    return LocalEntry(f"{FOLDER}/{path}" if path else FOLDER, size < 0, max(0, size), ms * NS, disk)


def _remote(
    id_: str,
    name: str,
    parent: str = "top",
    *,
    size: int = 3,
    ms: int = 10,
    md5: str | None = "same",
    owner: str | None = OWNER,
    mime: str | None = None,
    owned_by_me: bool | None = True,
) -> RemoteFile:
    return RemoteFile(
        id_,
        name,
        mime or (FOLDER_MIME if size < 0 else "text/plain"),
        (parent,),
        size if size >= 0 else None,
        md5 if size >= 0 else None,
        ms,
        {OWNER_PROPERTY: owner} if owner is not None else {},
        owned_by_me=owned_by_me,
    )


TOP = _remote("top", FOLDER, ROOT, size=-1)
LOCAL_TOP = _entry("", size=-1)


def _plan(
    local: Iterable[LocalEntry] = (),
    remote: Iterable[RemoteFile] = (),
    *,
    deep: bool = False,
    protected: tuple[str, ...] = (),
    patterns: tuple[str, ...] = (),
    hash_of: Callable[[LocalEntry], str | None] = lambda entry: "same",
    top: bool = True,
    mapped_id: Callable[[str, set[str]], str | None] = lambda _rel, _ids: None,
) -> Plan:
    entries = (LOCAL_TOP, *local)
    files = (TOP, *remote) if top else remote
    return make_plan(
        {entry.rel: entry for entry in entries},
        RemoteTree(ROOT, files),
        Matcher(patterns),
        hash_of,
        PlannerOptions((FOLDER,), OWNER, deep=deep, protected=protected),
        mapped_id=mapped_id,
    )


def _ops(plan: Plan) -> list[tuple[OpKind, str, str | None]]:
    return [(op.kind, op.rel, op.file_id) for op in plan.ops]


def test_empty_remote_creates_everything_in_order():
    plan = _plan([_entry("d/a"), _entry("z"), _entry("d", size=-1)], top=False)
    assert _ops(plan) == [
        (OpKind.MKDIR, FOLDER, None),
        (OpKind.MKDIR, "Projects/d", None),
        (OpKind.UPLOAD, "Projects/d/a", None),
        (OpKind.UPLOAD, "Projects/z", None),
    ]
    assert plan.folder_ids == {"": ROOT}
    assert [op.parent_rel for op in plan.ops] == ["", FOLDER, "Projects/d", FOLDER]


def test_identical_trees_produce_no_ops():
    plan = _plan([_entry("a")], [_remote("a", "a")])
    assert plan.is_empty


def test_unmanaged_identical_file_is_adopted():
    plan = _plan([_entry("a")], [_remote("a", "a", owner=None)])
    assert _ops(plan) == [(OpKind.ADOPT, "Projects/a", "a")]


def test_unmanaged_file_with_matching_metadata_requires_matching_hash():
    plan = _plan([_entry("a")], [_remote("a", "a", owner=None, md5="different")])
    assert _ops(plan) == [(OpKind.UPDATE, "Projects/a", "a")]


def test_size_change_updates():
    entry = _entry("café", size=7, disk="Projects/cafe\u0301")
    plan = _plan([entry], [_remote("a", "café")])
    assert _ops(plan) == [(OpKind.UPDATE, entry.rel, "a")]
    assert (plan.ops[0].size, plan.ops[0].disk_rel, plan.ops[0].reason) == (
        7,
        entry.disk,
        "changed",
    )


def test_same_size_time_change_same_md5_touches():
    plan = _plan([_entry("a", ms=11)], [_remote("a", "a")])
    assert _ops(plan) == [(OpKind.TOUCH, "Projects/a", "a")]
    assert plan.ops[0].mtime_ns == 11 * NS


def test_same_size_time_change_different_md5_updates():
    plan = _plan([_entry("a", ms=11)], [_remote("a", "a", md5="old")])
    assert _ops(plan) == [(OpKind.UPDATE, "Projects/a", "a")]
    assert plan.ops[0].md5 == "same"


def test_deep_mode_detects_silent_change():
    local, remote = [_entry("a")], [_remote("a", "a", md5="old")]
    assert _plan(local, remote).is_empty
    assert _ops(_plan(local, remote, deep=True)) == [(OpKind.UPDATE, "Projects/a", "a")]


def test_hash_unknown_means_update():
    calls = Counter()

    def unknown(entry):
        calls[entry.rel] += 1
        return None

    plan = _plan(
        [_entry("a", ms=11)],
        [_remote("a", "a"), _remote("b", "a")],
        hash_of=unknown,
    )
    assert _ops(plan) == [(OpKind.UPDATE, "Projects/a", "a"), (OpKind.TRASH, "Projects/a", "b")]
    assert plan.ops[0].md5 is None
    assert calls == {"Projects/a": 1}


def test_deleted_local_file_trashed():
    plan = _plan(remote=[_remote("a", "a"), _remote("u", "u", owner=None)])
    assert _ops(plan) == [(OpKind.TRASH, "Projects/a", "a"), (OpKind.TRASH, "Projects/u", "u")]
    assert [(op.reason, op.affected_files) for op in plan.ops] == [
        ("deleted-locally", 1),
        ("extraneous", 1),
    ]


def test_deleted_local_folder_trashed_once_with_affected_count():
    plan = _plan(
        remote=[
            _remote("d", "d", size=-1),
            _remote("sub", "sub", "d", size=-1),
            _remote("a", "a", "d"),
            _remote("b", "b", "sub"),
        ]
    )
    assert _ops(plan) == [(OpKind.TRASH, "Projects/d", "d")]
    assert plan.ops[0].affected_files == 2


def test_folder_with_native_doc_is_kept_and_children_trashed():
    plan = _plan(
        remote=[
            _remote("d", "d", size=-1),
            _remote("a", "a", "d"),
            _remote("g", "g", "d", mime="application/vnd.google-apps.document"),
        ]
    )
    assert _ops(plan) == [(OpKind.TRASH, "Projects/d/a", "a")]
    assert {(issue.code, issue.rel) for issue in plan.warnings} == {
        ("google-native-kept", "Projects/d/g"),
        ("folder-kept-protected", "Projects/d"),
    }


def test_excluded_remote_never_touched():
    plan = _plan(
        [_entry("omit", size=-1), _entry("omit/a")],
        [_remote("d", "omit", size=-1), _remote("a", "a", "d")],
        patterns=("omit/",),
    )
    assert plan.is_empty
    assert any(issue.code == "protected-kept" for issue in plan.warnings)


def test_protected_descendant_blocks_folder_move_and_trash():
    plan = _plan(
        [_entry("new", size=-1), _entry("new/a")],
        [_remote("old", "old", size=-1), _remote("a", "a", "old"), _remote("p", "keep", "old")],
        protected=("Projects/old/keep",),
    )
    assert _ops(plan) == [
        (OpKind.MKDIR, "Projects/new", None),
        (OpKind.MOVE, "Projects/new/a", "a"),
    ]
    assert plan.folder_ids["Projects/old"] == "old"


def test_out_of_scope_remote_never_touched():
    plan = _plan(
        [LocalEntry("Other/a", False, 3, 10 * NS)],
        [_remote("other", "Other", ROOT, size=-1, owner="foreign"), _remote("a", "a", "other")],
    )
    assert plan.is_empty
    assert [(issue.code, issue.rel) for issue in plan.warnings] == [("outside-scope", "Other")]
    assert not plan.errors


def test_type_conflict_file_vs_folder():
    plan = _plan(
        [_entry("d", size=-1), _entry("d/a"), _entry("f")],
        [
            _remote("d", "d"),
            _remote("f", "f", size=-1),
            _remote("old", "old", "f", size=9),
        ],
    )
    assert _ops(plan) == [
        (OpKind.MKDIR, "Projects/d", None),
        (OpKind.UPLOAD, "Projects/d/a", None),
        (OpKind.UPLOAD, "Projects/f", None),
        (OpKind.TRASH, "Projects/d", "d"),
        (OpKind.TRASH, "Projects/f", "f"),
    ]
    assert {op.reason for op in plan.ops if op.kind is OpKind.TRASH} == {"type-conflict"}


@pytest.mark.parametrize("barrier", ["protected", "excluded", "native"])
@pytest.mark.parametrize("owner", [OWNER, None])
def test_type_conflict_with_protected_folder_still_uploads_file(
    barrier: str, owner: str | None
) -> None:
    """Upload alongside the retained folder, clean eligible children and reach a fixed point."""
    entry = _entry("a")
    folder = _remote("d", "a", size=-1, owner=owner)
    kept = _remote(
        "p",
        "keep",
        "d",
        mime="application/vnd.google-apps.document" if barrier == "native" else None,
    )
    extra = _remote("x", "remove", "d")
    files = {item.id: item for item in (TOP, folder, kept, extra)}
    local = {item.rel: item for item in (LOCAL_TOP, entry)}
    matcher = Matcher(("keep",) if barrier == "excluded" else ())
    options = PlannerOptions(
        (FOLDER,),
        OWNER,
        deep=True,
        protected=("Projects/a/keep",) if barrier == "protected" else (),
    )
    warning = PlanIssue("type-conflict-kept", entry.rel)

    plan = make_plan(local, RemoteTree(ROOT, files.values()), matcher, lambda item: "same", options)
    assert _ops(plan) == [
        (OpKind.UPLOAD, "Projects/a", None),
        (OpKind.TRASH, "Projects/a/remove", "x"),
    ]
    assert plan.ops[0].reason == "new"
    assert warning in plan.warnings

    after = apply_plan(files, plan, local, lambda rel: "same", ROOT, OWNER)
    assert after[folder.id] == folder
    assert after[kept.id] == kept
    assert extra.id not in after
    second = make_plan(
        local, RemoteTree(ROOT, after.values()), matcher, lambda item: "same", options
    )
    assert not second.errors
    assert second.is_empty
    assert warning in second.warnings


def test_type_conflict_with_unmanaged_protected_folder_keeps_cleaning_eligible_children() -> None:
    """When the mirrored file already exists, an unmanaged retained folder still gets cleaned."""
    plan = _plan(
        [_entry("a")],
        [
            _remote("file", "a"),
            _remote("retained", "a", size=-1, owner=None),
            _remote("kept", "keep", "retained"),
            _remote("extra", "remove", "retained"),
        ],
        protected=("Projects/a/keep",),
    )
    assert _ops(plan) == [(OpKind.TRASH, "Projects/a/remove", "extra")]
    assert PlanIssue("type-conflict-kept", "Projects/a") in plan.warnings


@pytest.mark.parametrize(
    ("local_dir", "remote_mime", "owner", "owned_by_me", "notice"),
    [
        (False, "text/plain", OWNER, False, "foreign-item-kept"),
        (True, FOLDER_MIME, "another-mirror", True, "foreign-item-kept"),
        (True, "application/vnd.google-apps.document", "another-mirror", True, "foreign-item-kept"),
        (True, "application/vnd.google-apps.shortcut", None, True, "google-native-kept"),
        (False, FOLDER_MIME, OWNER, False, "foreign-item-kept"),
        (True, "text/plain", "another-mirror", True, "foreign-item-kept"),
    ],
    ids=["files", "folders", "document", "shortcut", "file-over-folder", "folder-over-file"],
)
def test_local_item_is_mirrored_alongside_protected_homonym(
    local_dir: bool, remote_mime: str, owner: str | None, owned_by_me: bool, notice: str
) -> None:
    """Keep the protected ID and back up the local path under a separate ID."""
    rel = "Projects/a"
    foreign = _remote(
        "a-foreign",
        "a",
        mime=remote_mime,
        size=-1 if remote_mime == FOLDER_MIME else 3,
        ms=20,
        md5="different",
        owner=owner,
        owned_by_me=owned_by_me,
    )
    preserved = {foreign.id: foreign}
    if foreign.is_folder:
        # Even a child with this mirror's marker is protected inside the foreign branch.
        child = _remote("foreign-child", "x", foreign.id, md5="different")
        preserved[child.id] = child
    entries = [_entry("a", size=-1), _entry("a/x")] if local_dir else [_entry("a")]
    expected = (
        [(OpKind.MKDIR, rel, None), (OpKind.UPLOAD, f"{rel}/x", None)]
        if local_dir
        else [(OpKind.UPLOAD, rel, None)]
    )
    local = {entry.rel: entry for entry in (LOCAL_TOP, *entries)}

    plan = _plan(entries, preserved.values(), deep=True)

    assert not plan.errors
    assert _ops(plan) == expected
    assert plan.warnings == [PlanIssue(notice, rel)]
    assert [op.parent_rel for op in plan.ops] == ([FOLDER, rel] if local_dir else [FOLDER])
    after = apply_plan({TOP.id: TOP, **preserved}, plan, local, lambda rel: "same", ROOT, OWNER)
    assert {file_id: after[file_id] for file_id in preserved} == preserved
    (backup,) = (
        item for item in after.values() if item.name == foreign.name and item.id != foreign.id
    )
    assert backup.parents == foreign.parents and backup.owner_root == OWNER
    if local_dir:
        (backed_up_child,) = (item for item in after.values() if item.parents == (backup.id,))
        assert backed_up_child.name == "x" and backed_up_child.md5 == "same"
    else:
        assert backup.md5 == "same"

    for listing in (list(after.values()), list(reversed(after.values()))):
        second = _plan(entries, listing, top=False, deep=True)
        assert not second.errors
        assert second.is_empty
        assert second.warnings == plan.warnings
        if local_dir:
            assert second.folder_ids[rel] == backup.id

    deleted = _plan([], after.values(), top=False)
    assert _ops(deleted) == [(OpKind.TRASH, rel, backup.id)]


def test_file_rename_is_move_not_upload():
    plan = _plan([_entry("new", ms=11)], [_remote("a", "old")])
    assert _ops(plan) == [(OpKind.MOVE, "Projects/new", "a")]
    op = plan.ops[0]
    assert (op.src_rel, op.src_parent_id, op.parent_rel, op.md5, op.mtime_ns) == (
        "Projects/old",
        "top",
        FOLDER,
        "same",
        11 * NS,
    )


def test_file_move_prefers_same_name():
    # The extra directory makes the old folder an unmatched structural candidate.
    plan = _plan(
        [_entry("new", size=-1), _entry("new/empty", size=-1), _entry("new/a")],
        [_remote("old", "old", size=-1), _remote("a", "a", "old"), _remote("b", "c")],
    )
    assert [(op.rel, op.file_id) for op in plan.ops if op.kind is OpKind.MOVE] == [
        ("Projects/new/a", "a")
    ]


def test_ambiguous_folder_move_falls_back():
    plan = _plan(
        [_entry("new", size=-1), _entry("new/a")],
        [
            _remote("x", "x", size=-1),
            _remote("y", "y", size=-1),
            _remote("xa", "a", "x"),
            _remote("ya", "a", "y"),
        ],
    )
    assert _ops(plan) == [
        (OpKind.MKDIR, "Projects/new", None),
        (OpKind.MOVE, "Projects/new/a", "xa"),
        (OpKind.TRASH, "Projects/x", "x"),
        (OpKind.TRASH, "Projects/y", "y"),
    ]
    assert plan.stats.trashed_files == 1


def test_folder_rename_is_single_move():
    plan = _plan(
        [_entry("new", size=-1), _entry("new/sub", size=-1), _entry("new/sub/a")],
        [
            _remote("old", "old", size=-1),
            _remote("sub", "sub", "old", size=-1),
            _remote("a", "a", "sub"),
        ],
    )
    assert _ops(plan) == [(OpKind.MOVE, "Projects/new", "old")]
    assert (plan.ops[0].mtime_ns, plan.ops[0].src_parent_id) == (0, "top")
    assert plan.folder_ids == {
        "": ROOT,
        FOLDER: "top",
        "Projects/new": "old",
        "Projects/new/sub": "sub",
    }


def test_folder_rename_with_inner_edit_moves_then_updates():
    plan = _plan(
        [_entry("new", size=-1), _entry("new/a"), _entry("new/b")],
        [
            _remote("old", "old", size=-1),
            _remote("a", "a", "old", md5="old"),
            _remote("b", "b", "old"),
        ],
    )
    assert _ops(plan) == [
        (OpKind.MOVE, "Projects/new", "old"),
        (OpKind.UPDATE, "Projects/new/a", "a"),
    ]


def test_nested_move_recomputes_the_old_ancestor_shape():
    """Move a child first, then move the remainder once its shape matches."""
    plan = _plan(
        [
            _entry("a-sub", size=-1),
            _entry("a-sub/a"),
            _entry("container", size=-1),
            _entry("container/rest", size=-1),
            _entry("container/rest/b", size=4),
        ],
        [
            _remote("old", "old", size=-1),
            _remote("sub", "sub", "old", size=-1),
            _remote("a", "a", "sub"),
            _remote("b", "b", "old", size=4),
        ],
    )
    assert _ops(plan) == [
        (OpKind.MKDIR, "Projects/container", None),
        (OpKind.MOVE, "Projects/a-sub", "sub"),
        (OpKind.MOVE, "Projects/container/rest", "old"),
    ]
    assert plan.folder_ids["Projects/container/rest"] == "old"
    assert "Projects/old" not in plan.folder_ids


def test_zero_byte_files_never_move_matched():
    plan = _plan([_entry("new", size=0)], [_remote("a", "old", size=0)])
    assert _ops(plan) == [
        (OpKind.UPLOAD, "Projects/new", None),
        (OpKind.TRASH, "Projects/old", "a"),
    ]


def test_epoch_file_move_converges_without_touch() -> None:
    """A file MOVE sets the epoch directly and keeps the original ID."""
    entry = _entry("new", ms=0)
    remote = _remote("a", "old")
    plan = _plan([entry], [remote])
    assert _ops(plan) == [(OpKind.MOVE, "Projects/new", "a")]
    assert plan.ops[0].mtime_ns == 0

    local = {item.rel: item for item in (LOCAL_TOP, entry)}
    files = {item.id: item for item in (TOP, remote)}
    after = apply_plan(files, plan, local, lambda rel: "same", ROOT, OWNER)
    assert after["a"].modified_ms == 0
    assert _plan([entry], after.values(), top=False, deep=True).is_empty


def test_epoch_dated_file_touch_converges() -> None:
    """A TOUCH to the epoch applies once when the bytes already match."""
    entry = _entry("a", ms=0)
    remote = _remote("a", "a")
    plan = _plan([entry], [remote])
    assert _ops(plan) == [(OpKind.TOUCH, "Projects/a", "a")]
    assert plan.ops[0].mtime_ns == 0

    local = {item.rel: item for item in (LOCAL_TOP, entry)}
    files = {item.id: item for item in (TOP, remote)}
    after = apply_plan(files, plan, local, lambda rel: "same", ROOT, OWNER)
    assert after["a"].modified_ms == 0
    assert _plan([entry], after.values(), top=False, deep=True).is_empty


@pytest.mark.parametrize("mtime_ns", [0, -1_234_567, 999_999, 7_123_456])
def test_file_ops_always_carry_local_mtime(mtime_ns: int) -> None:
    """Every kind of file write carries the unrounded local nanoseconds."""
    entries = [
        replace(_entry(name, size=size), mtime_ns=mtime_ns)
        for name, size in (("upload", 31), ("update", 7), ("touch", 3), ("adopt", 3), ("move", 17))
    ]
    plan = _plan(
        entries,
        [
            _remote("update", "update"),
            _remote("touch", "touch"),
            _remote("adopt", "adopt", ms=drive_ms(mtime_ns), owner=None),
            _remote("old", "old", size=17),
        ],
    )
    assert len(plan.ops) == len(entries)
    assert {op.rel: op.kind for op in plan.ops} == {
        "Projects/upload": OpKind.UPLOAD,
        "Projects/update": OpKind.UPDATE,
        "Projects/touch": OpKind.TOUCH,
        "Projects/adopt": OpKind.ADOPT,
        "Projects/move": OpKind.MOVE,
    }
    assert all(op.mtime_ns == mtime_ns for op in plan.ops)


def test_duplicates_managed_trashed_unmanaged_reported():
    plan = _plan(
        [_entry("a")],
        [
            _remote("good", "a", ms=9),
            _remote("bad", "a", ms=11, md5="orphan"),
            _remote("u", "a", owner=None),
        ],
    )
    assert _ops(plan) == [(OpKind.TOUCH, "Projects/a", "good"), (OpKind.TRASH, "Projects/a", "bad")]
    assert any(issue.code == "duplicate-unmanaged" for issue in plan.warnings)
    assert plan.ops[-1].reason == "duplicate"


def test_duplicate_folder_children_are_paired_under_the_retained_parent():
    plan = _plan(
        [_entry("d", size=-1), _entry("d/a")],
        [
            _remote("keep", "d", size=-1, ms=11),
            _remote("lose", "d", size=-1),
            _remote("a", "a", "lose"),
        ],
    )
    assert _ops(plan) == [
        (OpKind.UPLOAD, "Projects/d/a", None),
        (OpKind.TRASH, "Projects/d", "lose"),
    ]
    assert plan.folder_ids["Projects/d"] == "keep"
    assert plan.ops[-1].affected_files == 1


@pytest.mark.parametrize("matching", [False, True])
def test_duplicate_prefers_checksum_then_previous_mapping(matching: bool):
    plan = _plan(
        [_entry("report")],
        [
            _remote("previous", "report", ms=9, md5="same" if matching else "old"),
            _remote("newer", "report", ms=11),
        ],
        mapped_id=lambda _rel, _ids: "previous",
    )
    assert plan.ops[-1].file_id == ("newer" if matching else "previous")


def test_unknown_ownership_keeps_legacy_file_behavior():
    plan = _plan(
        [_entry("report", size=5)],
        [_remote("legacy", "report", owned_by_me=None)],
    )
    assert _ops(plan) == [(OpKind.UPDATE, "Projects/report", "legacy")]
    assert not plan.stats.per_folder_remote_files


def test_foreign_owner_is_kept():
    plan = _plan(
        [_entry("new")],
        [_remote("f", "f", owner="foreign", mime="application/vnd.google-apps.shortcut")],
        protected=("Projects/f",),
    )
    assert _ops(plan) == [(OpKind.UPLOAD, "Projects/new", None)]
    assert not plan.errors
    assert [(issue.code, issue.rel) for issue in plan.warnings] == [
        ("foreign-item-kept", "Projects/f")
    ]


def test_invalid_remote_names_trashed():
    plan = _plan(
        [_entry("new")],
        [_remote("slash", "slash/name"), _remote("dot", "."), _remote("nul", "x\x00")],
    )
    assert [op.kind for op in plan.ops] == [OpKind.UPLOAD] + [OpKind.TRASH] * 3
    assert {op.reason for op in plan.ops[1:]} == {"invalid-name"}


def test_nfc_and_nfd_names_match():
    assert _plan([_entry("café")], [_remote("a", "cafe\u0301")]).is_empty
    entry = _entry("café", disk="Projects/cafe\u0301")
    assert _plan([entry]).ops[0].disk_rel == entry.disk


def test_missing_local_parent_cannot_use_a_folder_about_to_be_trashed():
    """Reject an inconsistent local snapshot before writing to a doomed parent."""
    plan = _plan(
        [_entry("d/sub/a")],
        [_remote("d", "d", size=-1), _remote("sub", "sub", "d", size=-1)],
    )
    assert plan.is_empty
    assert [(issue.code, issue.rel) for issue in plan.errors] == [
        ("plan-inconsistent", "Projects/d/sub/a")
    ]


def test_hash_called_only_when_needed():
    entries = [
        _entry("unchanged"),
        _entry("new", size=51),
        _entry("new-size", size=52),
        _entry("dupe", ms=12),
        _entry("moved", size=21),
        _entry("mismatch", size=22),
    ]
    calls = Counter()

    def count(entry):
        calls[entry.rel] += 1
        return "unknown-match" if entry.rel.endswith("mismatch") else "same"

    plan = _plan(
        entries,
        [
            _remote("unchanged", "unchanged"),
            _remote("new-size", "new-size"),
            _remote("a", "dupe"),
            _remote("b", "dupe", md5="bad"),
            _remote("old", "old", size=21),
            _remote("old2", "old2", size=22),
        ],
        hash_of=count,
    )
    assert calls == {"Projects/dupe": 1, "Projects/moved": 1, "Projects/mismatch": 1}
    assert next(op for op in plan.ops if op.rel == "Projects/mismatch").md5 == "unknown-match"


def test_stats_and_folder_ids():
    plan = _plan(
        [_entry("new", size=20), _entry("big", size=9), _entry("d", size=-1)],
        [_remote("big", "big"), _remote("gone", "gone"), _remote("x", "x")],
        protected=("Projects/x",),
    )
    stats = plan.stats
    assert (stats.mkdirs, stats.uploads, stats.updates, stats.trashes, stats.trashed_files) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert stats.total_ops == len(plan.ops)
    assert stats.bytes_to_send == 29
    assert stats.per_folder_remote_files == {FOLDER: 2}
    assert stats.per_folder_trashed == {FOLDER: 1}
    assert stats.per_folder_pending_files == {FOLDER: 2}
    assert stats.per_folder_pending_bytes == {FOLDER: 29}
    assert plan.folder_ids == {"": ROOT, FOLDER: "top"}


def test_fingerprint_stable():
    remote = [_remote("x", "x\t\n"), _remote("y", "y")]
    first = _plan(remote=remote)
    second = _plan(remote=reversed(remote))
    assert first.ops == second.ops
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != _plan(remote=[replace(remote[0], name="x\t")]).fingerprint()
