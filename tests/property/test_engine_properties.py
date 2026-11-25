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

from nimbo.core.model import OWNER_PROPERTY, OpKind, OpState, RunResult, nfc, rel_parent
from nimbo.core.safety import read_sentinel
from tests.fakedrive import FakeFile, Fault, FaultPlan, SimulatedCrash
from tests.system.helpers import (
    ANCHOR,
    CHUNK_SIZE,
    LOCAL_ACTIONS,
    MULTIPART_MAX,
    REMOTE_ACTIONS,
    SMALL_TREE,
    EngineSession,
    View,
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
        self.owner = read_sentinel(self.mirror.root)
        assert self.owner is not None
        self.observed_run = 0
        self.backed_up: dict[str, tuple[int, str]] = {}
        self.undo_run: int | None = None
        self.undo_items: dict[str, FakeFile] = {}
        self.undo_roots: dict[str, str] = {}
        self.undo_failures: dict[str, str] = {}

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

    @precondition(lambda self: self.synced)
    @rule(seed=_SEED)
    def edit_drive_before_local_delete(self, seed: int) -> None:
        """Edit a backed-up Drive file, then remove its source before the next backup."""
        mirror = self.mirror
        paths = {
            nfc(path.relative_to(mirror.root).as_posix()): path
            for path in (mirror.root / "Docs").rglob("*")
            if path.is_file()
        }
        candidates = [
            (rel, item)
            for rel, item in live_items(mirror.fake, mirror.target).items()
            if rel != ANCHOR and rel in paths and item.id in self.backed_up
        ]
        if candidates:
            rel, item = Random(seed).choice(candidates)
            mirror.fake.set_content(item.id, item.content + b"\x00")
            paths[rel].unlink()
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
        matcher = mirror.config.matcher()
        folders = mirror.config.active_folders
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
            expected_remote = {
                rel: item
                for rel, item in self.undo_items.items()
                if any(rel == root or rel.startswith(f"{root}/") for root in allowed)
            }
            drive_only = {
                rel: code for rel, code in self.undo_failures.items() if rel in expected_remote
            }
            expected_local = {
                rel: item
                for rel, item in expected_remote.items()
                if not any(rel == path or rel.startswith(f"{path}/") for path in drive_only)
            }
            before_local = local_view(mirror.root, matcher, folders)
            blocked = self.undo_roots.keys() - allowed
            summary = session.engine.undo()
            assert summary.run_id == self.undo_run
            scope_failures = [(rel, "unsafe_restore_target") for rel in sorted(blocked)]
            assert summary.failed[: len(scope_failures)] == scope_failures, summary
            local_failures = summary.failed[len(scope_failures) :]
            assert sorted(local_failures) == sorted(drive_only.items()), summary
            assert not summary.skipped_existing, summary
            assert summary.untrashed == len(allowed), summary
            assert summary.restored_files == sum(item.is_binary for item in expected_local.values())
            for before in expected_remote.values():
                assert mirror.fake.file(before.id) == before
            assert not before_local.keys() & expected_remote.keys()
            restored_view = {
                rel: (item.is_folder, item.size or 0, item.md5)
                for rel, item in expected_local.items()
            }
            for rel in expected_local:
                parent = rel_parent(rel)
                while parent and parent not in before_local and parent not in restored_view:
                    restored_view[parent] = True, 0, None
                    parent = rel_parent(parent)
            assert local_view(mirror.root, matcher, folders) == before_local | restored_view
            for rel in blocked:
                assert mirror.fake.file(self.undo_roots[rel]).trashed
            self.undo_run = None
            self.undo_roots.clear()
            self.undo_items.clear()
            self.undo_failures.clear()
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

    def _observe_completed_ops(
        self,
        session: EngineSession,
        run_id: int,
        before: dict[str, tuple[str, FakeFile]],
        local: View,
    ) -> dict[str, str]:
        # The source view is the same for the interrupted run and its recovery.
        # Take digests from the source instead of the undo snapshots or Drive.
        trash_reasons = {}
        for journal_run in range(self.observed_run + 1, run_id + 1):
            for op, state, _ in session.index.op_states(journal_run):
                if state is not OpState.DONE or op.file_id is None:
                    continue
                if op.kind is OpKind.TRASH:
                    trash_reasons[op.file_id] = op.reason
                    continue
                previous = before.get(op.file_id)
                adopted = (
                    op.kind in (OpKind.ADOPT, OpKind.TOUCH)
                    and previous is not None
                    and OWNER_PROPERTY not in previous[1].properties
                )
                if op.kind in (OpKind.UPLOAD, OpKind.UPDATE) or adopted:
                    is_folder, size, md5 = local[op.rel]
                    if not is_folder:
                        assert md5 is not None
                        self.backed_up[op.file_id] = size, md5
        self.observed_run = run_id
        return trash_reasons

    def _undo_failure(self, item: FakeFile, trash_reason: str) -> str | None:
        backup = self.backed_up.get(item.id)
        if (
            item.owned_by_me is not True
            or item.properties.get(OWNER_PROPERTY) != self.owner
            or (item.is_binary and backup is None)
        ):
            return "not_backed_up"
        if trash_reason != "deleted-locally":
            return "not_deleted_locally"
        if item.is_binary and (item.size, item.md5) != backup:
            return "changed_since_backup"
        return None

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
        trash_reasons = self._observe_completed_ops(session, result.run_id, before, expected)
        records = session.index.trash_log(result.run_id)
        if records:
            self.undo_run = result.run_id
            self.undo_roots = {rel: file_id for file_id, rel, _ in records}
            self.undo_items = {}
            self.undo_failures = {}
            for file_id, rel, is_folder in records:
                original_rel, item = before[file_id]
                assert (rel, is_folder) == (original_rel, item.is_folder)
                assert mirror.fake.file(file_id) == replace(
                    item, trashed=True, explicitly_trashed=True
                )
                covered = [(rel, item)]
                if is_folder:
                    for child_rel, child in before.values():
                        if child_rel.startswith(f"{rel}/") and mirror.fake.file(child.id).trashed:
                            assert mirror.fake.file(child.id) == replace(child, trashed=True)
                            covered.append((child_rel, child))
                self.undo_items.update(covered)
                for path, child in sorted(covered, key=lambda pair: pair[0]):
                    if any(path.startswith(f"{parent}/") for parent in self.undo_failures):
                        continue
                    code = self._undo_failure(child, trash_reasons[file_id])
                    if code is not None:
                        self.undo_failures[path] = code
        mirror.assert_idempotent(session.engine)
        self.synced = True


def test_engine_state_machine(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Use the active Hypothesis profile to generate and shrink full engine sequences."""
    caplog.set_level(logging.WARNING, logger="httpx")
    # Hypothesis documents support for factory functions alongside state machine classes.
    run_state_machine_as_test(lambda: EngineMachine(tmp_path), settings=settings())  # type: ignore[arg-type]


@pytest.mark.parametrize("reason", ["not_backed_up", "changed_since_backup"])
def test_state_machine_undo_restores_drive_only(tmp_path: Path, reason: str) -> None:
    machine = EngineMachine(tmp_path)
    try:
        machine.backup()
        if reason == "not_backed_up":
            machine.drive_noise(action="extra", seed=0)
        else:
            machine.edit_drive_before_local_delete(seed=0)
        machine.backup()
        assert list(machine.undo_failures.values()) == [reason]
        machine.undo_last_deletions()
    finally:
        machine.teardown()


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
