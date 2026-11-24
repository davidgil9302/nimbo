"""Stateful properties of the engine, its durable journal, and Drive mirror."""

from __future__ import annotations

import logging
from dataclasses import replace
from math import ceil
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory

import httpx
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from nimbo.core.model import OpKind, RunResult
from tests.fakedrive import FakeFile, Fault, FaultPlan, SimulatedCrash
from tests.system.helpers import (
    CHUNK_SIZE,
    LOCAL_ACTIONS,
    MULTIPART_MAX,
    REMOTE_ACTIONS,
    SMALL_TREE,
    EngineSession,
    assert_no_duplicates,
    assert_ok,
    assert_untouched,
    count_writes,
    live_items,
    local_view,
    make_mirror,
    mutate_local,
    put_file,
    remote_noise,
    run_until_ok,
)

_SEED = st.integers(min_value=0, max_value=2**32 - 1)


class EngineMachine(RuleBasedStateMachine):
    """Mutate a private source and fake Drive, then back up, crash and restore."""

    def __init__(self, base: Path) -> None:
        """Create one clean fixture and its own journal for each generated sequence."""
        super().__init__()
        self.directory = TemporaryDirectory(prefix="engine-example-", dir=base)
        self.mirror = make_mirror(
            Path(self.directory.name),
            {**SMALL_TREE, "Docs/Re\u0301sume\u0301/example.txt": b"nested under NFD"},
            seed_docs=True,
        )
        self.serial = 0
        self.synced = False
        self.undo_run: int | None = None
        self.undo_items: dict[str, FakeFile] = {}
        self.undo_roots: dict[str, str] = {}

    def teardown(self) -> None:
        """Release the isolated files after Hypothesis finishes this example."""
        self.directory.cleanup()

    @rule(action=st.sampled_from(LOCAL_ACTIONS), seed=_SEED)
    def local_mutation(self, action: str, seed: int) -> None:
        """Change the source without changing the remote."""
        self.serial += 1
        if mutate_local(self.mirror.root, action, Random(seed), self.serial):
            self.synced = False

    @rule(action=st.sampled_from(REMOTE_ACTIONS), seed=_SEED)
    def drive_noise(self, action: str, seed: int) -> None:
        """External writes can disturb the mirror and add protected records."""
        self.serial += 1
        with self.mirror.open() as external:
            if remote_noise(self.mirror, external.drive, action, Random(seed), self.serial):
                self.synced = False

    @rule()
    def backup(self) -> None:
        """A successful run must converge with a subsequent write-free run."""
        with self.mirror.open() as session:
            self._finish_backup(session, self._remote_before())

    @precondition(lambda self: not self.synced)
    @rule(data=st.data())
    def crash_after_write(self, data: st.DataObject) -> None:
        """Apply one random planned write, drop the process and reopen the same journal."""
        mirror = self.mirror
        before = self._remote_before()
        start = len(mirror.fake.calls)
        with mirror.open() as session:
            preview = session.engine.preview()
            assert not preview.plan.errors, preview.plan.errors
            assert count_writes(mirror.fake, start) == 0
            writes = sum(
                1
                + (
                    ceil(op.size / CHUNK_SIZE)
                    if op.kind in (OpKind.UPLOAD, OpKind.UPDATE) and op.size > MULTIPART_MAX
                    else 0
                )
                for op in preview.plan.ops
            )
            if writes:
                ordinal = data.draw(st.integers(min_value=1, max_value=writes), label="write")
                mirror.fake.faults.add(Fault(FaultPlan.is_write, "crash_after", skip=ordinal - 1))
                with pytest.raises(SimulatedCrash):
                    session.engine.run(trigger="manual")
                assert count_writes(mirror.fake, start) == ordinal
        assert_no_duplicates(mirror.fake, mirror.target)
        assert_untouched(mirror.fake, mirror.protected)
        with mirror.open() as recovered:
            assert recovered.index.integrity_ok()
            self._finish_backup(recovered, before)

    @precondition(lambda self: self.synced and self.undo_run is not None)
    @rule()
    def undo_last_deletions(self) -> None:
        """Restore the last journaled deletions and verify their bytes and original IDs."""
        mirror = self.mirror
        with mirror.open() as session:
            assert session.index.last_run_with_trash() == self.undo_run
            live = {
                mirror.target,
                *(item.id for item in live_items(mirror.fake, mirror.target).values()),
            }
            allowed = {
                rel
                for rel, file_id in self.undo_roots.items()
                if mirror.fake.file(file_id).parent_id in live
            }
            expected = {
                rel: item
                for rel, item in self.undo_items.items()
                if any(rel == root or rel.startswith(f"{root}/") for root in allowed)
            }
            summary = session.engine.undo()
            assert summary.run_id == self.undo_run
            assert summary.failed == [
                (rel, "unsafe_restore_target") for rel in sorted(self.undo_roots.keys() - allowed)
            ], summary
            assert not summary.skipped_existing, summary
            assert summary.untrashed == len(allowed), summary
            assert summary.restored_files == sum(item.is_binary for item in expected.values())
            local = local_view(mirror.root, mirror.config.matcher(), mirror.config.active_folders)
            for rel, before in expected.items():
                assert mirror.fake.file(before.id) == before
                assert local.get(rel) == (before.is_folder, before.size or 0, before.md5), rel
            for rel in self.undo_roots.keys() - allowed:
                assert mirror.fake.file(self.undo_roots[rel]).trashed
            self.undo_run = None
            self.undo_roots.clear()
            self.undo_items.clear()
            # Restored items from other Drive clients may be adopted on this backup.
            self._finish_backup(session, self._remote_before())

    @invariant()
    def protected_items_survive(self) -> None:
        """Even a dirty mirror must preserve protected records and avoid duplicates."""
        assert_untouched(self.mirror.fake, self.mirror.protected)
        assert_no_duplicates(self.mirror.fake, self.mirror.target)

    def _remote_before(self) -> dict[str, tuple[str, FakeFile]]:
        return {
            item.id: (rel, item)
            for rel, item in live_items(self.mirror.fake, self.mirror.target).items()
        }

    def _finish_backup(
        self, session: EngineSession, before: dict[str, tuple[str, FakeFile]]
    ) -> None:
        mirror = self.mirror
        matcher = mirror.config.matcher()
        folders = mirror.config.active_folders
        expected = local_view(mirror.root, matcher, folders)
        result = run_until_ok(session.engine)
        assert result.run_id is not None
        mirror.assert_state()
        assert local_view(mirror.root, matcher, folders) == expected
        records = session.index.trash_log(result.run_id)
        if records:
            self.undo_run = result.run_id
            self.undo_roots = {rel: file_id for file_id, rel, _ in records}
            self.undo_items = {}
            for file_id, rel, is_folder in records:
                original_rel, item = before[file_id]
                assert (rel, is_folder) == (original_rel, item.is_folder)
                assert mirror.fake.file(file_id) == replace(
                    item, trashed=True, explicitly_trashed=True
                )
                self.undo_items[rel] = item
                if is_folder:
                    for child_rel, child in before.values():
                        if child_rel.startswith(f"{rel}/") and mirror.fake.file(child.id).trashed:
                            assert mirror.fake.file(child.id) == replace(child, trashed=True)
                            self.undo_items[child_rel] = child
        mirror.assert_idempotent(session.engine)
        self.synced = True


def test_engine_state_machine(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Use the active Hypothesis profile to generate and shrink full engine sequences."""
    caplog.set_level(logging.WARNING, logger="httpx")
    # Hypothesis documents support for factory functions alongside state machine classes.
    run_state_machine_as_test(lambda: EngineMachine(tmp_path), settings=settings())  # type: ignore[arg-type]


def test_remote_edit_with_same_size_and_mtime_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair an externally edited file whose bytes changed without changing its metadata."""
    rel = "Docs/a.txt"
    mirror = make_mirror(tmp_path, {rel: b"abc"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        item = live_items(mirror.fake, mirror.target)[rel]

        def unexpected_hash(*args: object, **kwargs: object) -> None:
            pytest.fail("planner read unchanged local bytes despite a prior verified upload")

        monkeypatch.setattr("nimbo.core.hasher._md5_stream", unexpected_hash)
        session.drive.multipart_upload(
            data=b"xyz",
            file_id=item.id,
            name=None,
            parent_id=None,
            modified_ms=item.modified_ms,
            properties=None,
            update=True,
        )
        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
        mirror.assert_idempotent(session.engine)


def test_move_out_during_resumable_update_preserves_remote_bytes(tmp_path: Path) -> None:
    """Preserve a moved-out file when an external client moves it between upload chunks."""
    rel = "Docs/a.bin"
    mirror = make_mirror(tmp_path, {rel: b"original"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        items = live_items(mirror.fake, mirror.target)
        item, folder = items[rel], items["Docs"]
        put_file(mirror.root / rel, b"x" * (CHUNK_SIZE + 1))
        moved = False
        media_ranges: list[str] = []

        def move_while_sending_first_chunk(request: httpx.Request) -> bool:
            nonlocal moved
            if request.method != "PUT" or not request.content:
                return False
            media_ranges.append(request.headers["Content-Range"])
            if len(media_ranges) == 1 and not moved:
                session.drive.update_metadata(
                    item.id, add_parent=mirror.outside, remove_parents=[folder.id]
                )
                mirror.protect(item.id)
                moved = True
            return False

        mirror.fake.faults.add(Fault(move_while_sending_first_chunk, "delay"))
        try:
            summary = session.engine.run(trigger="manual")
        finally:
            mirror.fake.faults.clear()
        assert moved
        assert_untouched(mirror.fake, mirror.protected)
        assert media_ranges == [f"bytes 0-{CHUNK_SIZE - 1}/{CHUNK_SIZE + 1}"]
        assert summary.result is RunResult.PARTIAL and summary.failed == [(rel, "remote_changed")]
        run_until_ok(session.engine)
        mirror.assert_state()
        mirror.assert_idempotent(session.engine)


def test_undo_preserves_restored_file_under_decomposed_parent(tmp_path: Path) -> None:
    """Restore into the existing disk path when its spelling differs from the Drive name."""
    parent = "Docs/Re\u0301sume\u0301"
    rel = f"{parent}/removed.txt"
    mirror = make_mirror(tmp_path, {f"{parent}/keep.txt": b"keep", rel: b"gone"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        (mirror.root / rel).unlink()
        deleted = session.engine.run(trigger="manual")
        assert_ok(deleted)
        assert deleted.trashed == 1
        undo = session.engine.undo()
        assert undo.restored_files == 1 and not undo.failed, undo
        assert_ok(session.engine.run(trigger="manual"))
        assert "Docs/Résumé/removed.txt" in mirror.fake.snapshot(mirror.target)
        assert (mirror.root / rel).read_bytes() == b"gone"
        mirror.assert_state()
        mirror.assert_idempotent(session.engine)


def test_committed_session_does_not_reuse_id_after_file_moves_out(tmp_path: Path) -> None:
    """Recreate a local file after its previous committed upload ID leaves the mirror."""
    rel = "Docs/a.bin"
    final_range = f"bytes {CHUNK_SIZE}-{CHUNK_SIZE}/{CHUNK_SIZE + 1}"
    mirror = make_mirror(tmp_path, {rel: CHUNK_SIZE + 1})
    mirror.fake.faults.add(
        Fault(lambda request: request.headers.get("Content-Range") == final_range, "crash_after")
    )
    with mirror.open() as interrupted, pytest.raises(SimulatedCrash):
        interrupted.engine.run(trigger="manual")

    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        items = live_items(mirror.fake, mirror.target)
        original = items[rel]
        session.drive.update_metadata(
            original.id, add_parent=mirror.outside, remove_parents=[items["Docs"].id]
        )
        mirror.protect(original.id)

        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
        assert live_items(mirror.fake, mirror.target)[rel].id != original.id
        mirror.assert_idempotent(session.engine)


def test_undo_skips_existing_equivalent_leaf(tmp_path: Path) -> None:
    """Keep an existing local file even when its disk name is decomposed."""
    rel = "Docs/Nested/Café.txt"
    mirror = make_mirror(tmp_path, {"Docs/keep.txt": b"keep", rel: b"original"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        (mirror.root / rel).unlink()
        assert_ok(session.engine.run(trigger="manual"))
        replacement = mirror.root / "Docs/Nested/Cafe\u0301.txt"
        put_file(replacement, b"local replacement")

        undo = session.engine.undo()
        assert undo.skipped_existing == [rel] and undo.restored_files == 0 and not undo.failed
        assert replacement.read_bytes() == b"local replacement"
        assert not (mirror.root / rel).exists()
        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
