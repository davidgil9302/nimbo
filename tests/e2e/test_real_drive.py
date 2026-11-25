"""Exercise the mirror against the opt-in Google Drive sandbox."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Collection
from contextlib import closing
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from unicodedata import normalize

import pytest

from nimbo.core.index import META_CHANGES_PROBE, META_CHANGES_RELIABLE, Index
from nimbo.core.model import OWNER_PROPERTY, RemoteFile, RunResult
from nimbo.core.safety import write_approval
from tests.e2e.conftest import DOC_MIME, DriveCase, DriveTraffic
from tests.system.helpers import build_tree, local_view

pytestmark = pytest.mark.e2e

MIB = 1 << 20
ANCHOR = "Projects/keep.txt"


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _items(case: DriveCase, *, ignored: Collection[str] = ()) -> dict[str, RemoteFile]:
    """List by physical parent and reject duplicate normalized paths."""
    items = {}
    seen = {case.target_id}
    pending = [("", case.target_id)]
    while pending:
        prefix, parent_id = pending.pop()
        for item in case.drive.list_children([parent_id]):
            if item.id in ignored:
                continue
            rel = prefix + normalize("NFC", item.name)
            assert item.parents == (parent_id,) and not item.trashed, rel
            assert rel not in items and item.id not in seen, rel
            items[rel] = item
            seen.add(item.id)
            if item.is_folder:
                pending.append((f"{rel}/", item.id))
    return items


def _assert_mirror(case: DriveCase, *, ignored: Collection[str] = ()) -> dict[str, RemoteFile]:
    """Compare live Drive metadata against a fresh local listing and content digests."""
    items = _items(case, ignored=ignored)
    remote = {
        rel: (True, 0, None) if item.is_folder else (False, item.size, item.md5)
        for rel, item in items.items()
        if not item.is_google_native
    }
    cfg = case.config
    assert remote == local_view(cfg.source.root, cfg.matcher(), cfg.active_folders)
    return items


def _settled_state(item: RemoteFile) -> tuple[object, ...]:
    # Google Docs update their size and modifiedTime after creation, so compare only
    # what the tests wrote: placement, name, type and, for binaries, the content checksum.
    return (item.name, item.parents, item.trashed, item.mime_type, item.md5)


def _wait_for_drive_changes(case: DriveCase, start_cursor: str, *items: RemoteFile) -> None:
    """Re-read from before the writes until the feed shows each expected file state."""
    expected = {item.id: item for item in items}
    pending = expected.copy()
    timeout = 60
    deadline = monotonic() + timeout
    while pending and monotonic() < deadline:
        pending = expected.copy()
        cursor: str | None = start_cursor
        seen: set[str] = set()
        while cursor is not None and cursor not in seen and monotonic() < deadline:
            seen.add(cursor)
            page = case.drive.changes(cursor)
            for change in page.changes:
                item = change.file
                wanted = pending.get(change.file_id)
                if (
                    not change.removed
                    and item is not None
                    and wanted is not None
                    and _settled_state(item) == _settled_state(wanted)
                ):
                    del pending[change.file_id]
            if not pending:
                return
            cursor = page.next_page_token
        sleep(max(0, min(1, deadline - monotonic())))
    if pending:
        missing = ", ".join(sorted(pending))
        pytest.fail(
            f"The Drive changes feed did not report matching metadata for file IDs {missing} "
            f"within {timeout} seconds"
        )


def test_varied_tree_uses_resumable_upload(drive_case: DriveCase) -> None:
    """Mirror nested names, Unicode, an empty file and a 30 MiB binary."""
    build_tree(
        drive_case.config.source.root,
        {
            "Projects/Café/nested/empty.txt": b"",
            "Projects/Re\u0301sume\u0301/notes with spaces.txt": b"notes",
            "Projects/📁/sample.bin": 30 * MIB,
            "Projects/empty folder": None,
        },
    )
    traffic = DriveTraffic()
    summary = drive_case.run(traffic=traffic)

    assert summary.uploaded == 4
    assert traffic.starts == 1
    assert traffic.offsets[:2] == [0, drive_case.config.transfer.chunk_mib * MIB]
    _assert_mirror(drive_case)


def test_second_run_makes_no_drive_writes(drive_case: DriveCase) -> None:
    """Count HTTP mutation attempts on the second run, even if an attempt fails."""
    drive_case.run()
    traffic = DriveTraffic()

    drive_case.run(traffic=traffic)

    assert traffic.writes == 0
    _assert_mirror(drive_case)


def test_folder_move_preserves_ids_and_sends_no_file_bytes(drive_case: DriveCase) -> None:
    """Rename a nested folder and change its parent in one server-side move."""
    root = drive_case.config.source.root
    old = "Projects/Original"
    new = "Projects/Destination/Renamed"
    build_tree(root, {f"{old}/payload.bin": b"payload", "Projects/Destination": None})
    drive_case.run()
    before = _assert_mirror(drive_case)

    (root / old).rename(root / new)
    summary = drive_case.run()

    assert (summary.moved, summary.uploaded, summary.updated, summary.uploaded_bytes) == (
        1,
        0,
        0,
        0,
    )
    after = _assert_mirror(drive_case)
    assert after[new].id == before[old].id
    assert after[new].parents == (before["Projects/Destination"].id,)
    assert after[f"{new}/payload.bin"].id == before[f"{old}/payload.bin"].id


def test_delete_then_undo_restores_remote_id_and_local_bytes(drive_case: DriveCase) -> None:
    """Trash a deleted binary and undo that run through the engine."""
    rel = "Projects/removable.txt"
    path = drive_case.config.source.root / rel
    path.write_bytes(b"restore me")
    drive_case.run()
    item = _assert_mirror(drive_case)[rel]
    path.unlink()

    summary = drive_case.run()

    assert summary.trashed == 1 and summary.run_id is not None
    trashed = drive_case.drive.get(item.id)
    assert trashed is not None and trashed.trashed
    with drive_case.engine() as engine:
        restored = engine.undo(summary.run_id)
    assert (restored.untrashed, restored.restored_files, restored.failed) == (1, 1, [])
    assert path.read_bytes() == b"restore me"
    assert _assert_mirror(drive_case)[rel].id == item.id


def test_deletion_brake_requires_and_consumes_approval(drive_case: DriveCase) -> None:
    """Block two deletes until the current plan's fingerprint is approved."""
    root = drive_case.config.source.root
    paths = ("Projects/remove-a.txt", "Projects/remove-b.txt")
    build_tree(root, dict.fromkeys(paths, b"removable"))
    drive_case.run()
    before = _assert_mirror(drive_case)
    for rel in paths:
        (root / rel).unlink()
    traffic = DriveTraffic()

    blocked = drive_case.run(expected=RunResult.NEEDS_CONFIRMATION, traffic=traffic)

    assert blocked.fingerprint and blocked.trash_preview == [f"{rel}\t1" for rel in paths]
    assert traffic.writes == 0
    assert set(paths) <= _items(drive_case).keys()
    with drive_case.engine() as engine:
        decision = engine.preview().brake
    assert decision.tripped and decision.fingerprint == blocked.fingerprint
    write_approval(drive_case.paths.approval_file, decision.fingerprint, now=datetime.now(UTC))

    assert drive_case.run().trashed == 2
    assert not drive_case.paths.approval_file.exists()
    for rel in paths:
        item = drive_case.drive.get(before[rel].id)
        assert item is not None and item.trashed, rel
    _assert_mirror(drive_case)


def test_external_content_edit_is_overwritten_on_next_run(drive_case: DriveCase) -> None:
    """Repair a same-size Drive edit even when it preserves the file mtime."""
    drive_case.run()
    original = _assert_mirror(drive_case)[ANCHOR]
    start_cursor = drive_case.drive.start_page_token()
    edited = drive_case.write_binary(original, data=b"edit", properties={"e2e-editor": "external"})
    assert edited.properties.get("e2e-editor") == "external"
    assert (edited.size, edited.modified_ms, edited.md5) == (
        original.size,
        original.modified_ms,
        _md5(b"edit"),
    )
    _wait_for_drive_changes(drive_case, start_cursor, edited)

    summary = drive_case.run()

    assert (summary.updated, summary.uploaded) == (1, 0)
    assert _assert_mirror(drive_case)[ANCHOR].id == original.id


def test_duplicate_names_keep_the_matching_file(drive_case: DriveCase) -> None:
    """Trash a managed duplicate while preserving the matching and unmanaged files."""
    drive_case.run()
    original = _assert_mirror(drive_case)[ANCHOR]
    assert original.owner_root is not None
    start_cursor = drive_case.drive.start_page_token()
    duplicate = drive_case.write_binary(
        original,
        data=b"managed duplicate",
        properties={OWNER_PROPERTY: original.owner_root},
        duplicate=True,
    )
    foreign = drive_case.write_binary(
        original, data=b"unmanaged duplicate", properties={}, duplicate=True
    )
    assert len({original.id, duplicate.id, foreign.id}) == 3
    assert duplicate.name == foreign.name == original.name
    _wait_for_drive_changes(drive_case, start_cursor, duplicate, foreign)

    summary = drive_case.run()

    assert (summary.trashed, summary.uploaded, summary.updated) == (1, 0, 0)
    assert any(w.code == "duplicate-unmanaged" and w.rel == ANCHOR for w in summary.warnings)
    discarded = drive_case.drive.get(duplicate.id)
    assert discarded is not None and discarded.trashed
    preserved = drive_case.drive.get(foreign.id)
    assert preserved is not None and not preserved.trashed
    assert (preserved.parents, preserved.name, preserved.md5, preserved.owner_root) == (
        foreign.parents,
        foreign.name,
        _md5(b"unmanaged duplicate"),
        None,
    )
    assert _assert_mirror(drive_case, ignored={foreign.id})[ANCHOR].id == original.id


def test_google_doc_can_share_the_name_of_a_local_folder(drive_case: DriveCase) -> None:
    """Back up a local subtree beside a Google Doc with the same name."""
    build_tree(drive_case.config.source.root, {"Projects/nested/report.txt": b"local report"})
    start_cursor = drive_case.drive.start_page_token()
    doc = drive_case.create_doc(drive_case.target_id, name="Projects")
    _wait_for_drive_changes(drive_case, start_cursor, doc)

    summary = drive_case.run()

    assert (summary.mkdirs, summary.uploaded) == (2, 2)
    items = _assert_mirror(drive_case, ignored={doc.id})
    backup = items["Projects"]
    assert backup.is_folder and backup.owner_root is not None
    assert backup.parents == doc.parents == (drive_case.target_id,)
    preserved = drive_case.drive.get(doc.id)
    assert preserved is not None
    assert _settled_state(preserved) == _settled_state(doc)
    assert preserved.properties == doc.properties and preserved.owned_by_me == doc.owned_by_me

    traffic = DriveTraffic()
    drive_case.run(traffic=traffic)

    assert traffic.writes == 0
    assert _assert_mirror(drive_case, ignored={doc.id})["Projects"].id == backup.id


def test_google_doc_protects_locally_deleted_folder(drive_case: DriveCase) -> None:
    """Keep an actual Google Doc and its parent while trashing a deleted binary sibling."""
    root = drive_case.config.source.root
    folder = "Projects/Removable"
    rel = f"{folder}/binary.txt"
    build_tree(root, {rel: b"discard"})
    drive_case.run()
    before = _assert_mirror(drive_case)
    start_cursor = drive_case.drive.start_page_token()
    doc = drive_case.create_doc(before[folder].id)
    assert doc.mime_type == DOC_MIME and doc.parents == (before[folder].id,)
    (root / rel).unlink()
    (root / folder).rmdir()
    _wait_for_drive_changes(drive_case, start_cursor, doc)

    summary = drive_case.run()

    assert summary.trashed == 1
    discarded = drive_case.drive.get(before[rel].id)
    assert discarded is not None and discarded.trashed
    after = _items(drive_case)
    assert set(after) == {"Projects", ANCHOR, folder, f"{folder}/{doc.name}"}
    assert after[folder].id == before[folder].id
    preserved = after[f"{folder}/{doc.name}"]
    assert (preserved.id, preserved.mime_type, preserved.parents) == (doc.id, DOC_MIME, doc.parents)


def test_changes_feed_reports_our_writes_and_satisfies_probe(drive_case: DriveCase) -> None:
    """Read the new binary from the real changes feed and verify engine probe state."""
    start_cursor = drive_case.drive.start_page_token()
    drive_case.run()
    item = _assert_mirror(drive_case)[ANCHOR]
    _wait_for_drive_changes(drive_case, start_cursor, item)

    drive_case.run()
    with closing(Index.open_readonly(drive_case.paths.db_file)) as index:
        assert index.get_meta(META_CHANGES_RELIABLE) == "1"
        assert index.get_meta(META_CHANGES_PROBE) is None


@pytest.mark.skipif(os.environ.get("NIMBO_E2E_LARGE") != "1", reason="Set NIMBO_E2E_LARGE=1")
@pytest.mark.timeout(3600)
def test_interrupted_2_gib_upload_resumes_after_confirmed_prefix(drive_case: DriveCase) -> None:
    """Stop on a real 308 acknowledgement, reopen the engine and send only the suffix."""
    drive_case.run()
    total = 2 << 30
    path = drive_case.config.source.root / "Projects/large.bin"
    with path.open("wb") as stream:
        stream.truncate(total)
    interrupted = DriveTraffic(stop=Event())

    drive_case.run(expected=RunResult.CANCELLED, traffic=interrupted)

    assert interrupted.starts == 1
    assert interrupted.offsets == [0]
    assert 0 < interrupted.confirmed < total
    resumed = DriveTraffic()

    summary = drive_case.run(traffic=resumed)

    assert resumed.starts == 0 and resumed.probes >= 1
    assert resumed.offsets and resumed.offsets[0] == interrupted.confirmed
    assert min(resumed.offsets) == interrupted.confirmed
    assert summary.uploaded == 1 and summary.uploaded_bytes >= total - interrupted.confirmed
    _assert_mirror(drive_case)


@pytest.mark.skipif(
    not os.environ.get("NIMBO_E2E_COMPUTERS_FOLDER_ID", "").strip(),
    reason="Set NIMBO_E2E_COMPUTERS_FOLDER_ID",
)
def test_computers_folder_can_mirror_and_update(computers_case: DriveCase) -> None:
    """Create and then update a mirror below a folder created by Drive for desktop."""
    assert computers_case.run().uploaded == 1
    before = _assert_mirror(computers_case)[ANCHOR]

    (computers_case.config.source.root / ANCHOR).write_bytes(b"updated fixture")

    assert computers_case.run().updated == 1
    assert _assert_mirror(computers_case)[ANCHOR].id == before.id
