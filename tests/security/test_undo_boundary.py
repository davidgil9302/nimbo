"""Undo can restore Drive items without trusting them as local backups."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from stat import S_IMODE

import pytest
from textual.widgets import Static

from nimbo.app.service import ServiceState
from nimbo.app.tui.app import NimboApp
from nimbo.app.tui.screens.history import HistoryScreen, RunDetailScreen, UndoScreen
from nimbo.app.wizard import META_UNDO_BOUNDARY
from nimbo.core.config import save_config
from nimbo.core.model import Op, OpKind, RemoteFile
from tests.system.helpers import FIXED_TIME, assert_ok, live_items, make_mirror, put_file

_ANCHOR = "Docs/anchor"


@pytest.mark.parametrize("inside_folder", [False, True], ids=["file", "folder"])
def test_undo_keeps_another_owners_extra_file_in_drive(tmp_path: Path, inside_folder: bool) -> None:
    report = "Docs/old/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", report: b"backup"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        items = live_items(mirror.fake, mirror.target)
        if inside_folder:
            (mirror.root / report).unlink()
            (mirror.root / report).parent.rmdir()
            run_id = session.engine.run(trigger="manual").run_id
            assert run_id is not None
            folder_id = items["Docs/old"].id
            assert mirror.fake.file(folder_id).trashed
            # A collaborator added this child after Nimbo took the trash snapshot.
            session.drive.update_metadata(folder_id, trashed=False)
            extra_id = mirror.fake.add_file("extra", folder_id, b"shared", owned_by_me=False)
            session.drive.update_metadata(folder_id, trashed=True)
            rel = "Docs/old/extra"
        else:
            # Current runs protect foreign-owned files. Model a historical trash log.
            rel = "Docs/extra"
            extra_id = mirror.fake.add_file("extra", items["Docs"].id, b"shared", owned_by_me=False)
            run_id = session.index.start_run("manual", FIXED_TIME.isoformat())
            op = Op(OpKind.TRASH, rel, "extraneous", file_id=extra_id)
            session.index.save_ops(run_id, [op])
            session.drive.update_metadata(extra_id, trashed=True)
            session.index.complete_op(op, trash_log_run=run_id)

        assert mirror.fake.file(extra_id).trashed
        result = session.engine.undo(run_id)

    assert not mirror.fake.file(extra_id).trashed
    assert not (mirror.root / rel).exists()
    assert result.failed == [(rel, "not_backed_up")]
    if inside_folder:
        assert (mirror.root / report).read_bytes() == b"backup"


def test_undo_keeps_content_edited_before_trash_in_drive(tmp_path: Path) -> None:
    rel = "Docs/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: b"original"})
    path = mirror.root / rel
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        item = live_items(mirror.fake, mirror.target)[rel]
        mirror.fake.set_content(item.id, b"modified")
        path.unlink()
        trashed = session.engine.run(trigger="manual")
        assert trashed.trashed == 1 and mirror.fake.file(item.id).trashed

        result = session.engine.undo(trashed.run_id)

    assert not mirror.fake.file(item.id).trashed
    assert mirror.fake.file(item.id).content == b"modified"
    assert not path.exists()
    assert result.failed == [(rel, "changed_since_backup")]


@pytest.mark.parametrize("backup", ["upload", "update", "adopt", "touch"])
def test_undo_restores_a_file_deleted_locally(tmp_path: Path, backup: str) -> None:
    rel = "Docs/report"
    original = b"original"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: original})
    path = mirror.root / rel
    expected = original
    with mirror.open() as session:
        if backup in ("adopt", "touch"):
            docs_id = mirror.fake.add_folder("Docs", mirror.target)
            stamp = path.stat().st_mtime_ns // 1_000_000
            mirror.fake.add_file(
                "report", docs_id, original, modified_ms=stamp - (1000 if backup == "touch" else 0)
            )
        initial = session.engine.run(trigger="manual")
        assert_ok(initial)
        if backup == "adopt":
            assert initial.adopted == 1
        elif backup == "touch":
            assert initial.touched == 1
        elif backup == "update":
            expected = b"updated bytes"
            put_file(path, expected)
            updated = session.engine.run(trigger="manual")
            assert_ok(updated)
            assert updated.updated == 1

    with mirror.open() as session:
        assert session.engine.verify().mismatched == []  # Full listings preserve backup evidence.
        path.unlink()
        trashed = session.engine.run(trigger="manual")

        result = session.engine.undo(trashed.run_id)

    assert result.failed == [] and result.restored_files == 1
    assert path.read_bytes() == expected


def test_undo_keeps_a_trashed_duplicate_of_a_backed_up_file_in_drive(tmp_path: Path) -> None:
    rel = "Docs/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: b"report"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        duplicate = live_items(mirror.fake, mirror.target)[rel]
        session.drive.update_metadata(duplicate.id, name="anchor")
        (mirror.root / rel).unlink()
        trashed = session.engine.run(trigger="manual")
        assert trashed.trashed == 1 and mirror.fake.file(duplicate.id).trashed
        (mirror.root / _ANCHOR).unlink()

        result = session.engine.undo(trashed.run_id)

    assert not mirror.fake.file(duplicate.id).trashed
    assert not (mirror.root / _ANCHOR).exists()
    assert result.failed == [(_ANCHOR, "not_deleted_locally")]


def test_undo_requires_drive_ownership_at_trash_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Docs/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: b"report"})
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        item = live_items(mirror.fake, mirror.target)[rel]
        update = session.drive.update_metadata

        def omit_ownership_at_trash(file_id: str, **kwargs: object) -> RemoteFile:
            current = update(file_id, **kwargs)
            if file_id == item.id and kwargs.get("trashed") is True:
                return replace(current, owned_by_me=None)
            return current

        monkeypatch.setattr(session.drive, "update_metadata", omit_ownership_at_trash)
        (mirror.root / rel).unlink()
        trashed = session.engine.run(trigger="manual")
        assert trashed.trashed == 1 and mirror.fake.file(item.id).trashed

        result = session.engine.undo(trashed.run_id)

    assert not mirror.fake.file(item.id).trashed
    assert not (mirror.root / rel).exists()
    assert result.failed == [(rel, "not_backed_up")]


@pytest.mark.parametrize("umask", [0o022, 0o777], ids=["022", "777"])
def test_undo_creates_private_directories(tmp_path: Path, umask: int) -> None:
    rel = "Docs/old/nested/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: b"original"})
    path = mirror.root / rel
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        path.unlink()
        path.parent.rmdir()
        path.parent.parent.rmdir()
        trashed = session.engine.run(trigger="manual")
        # Also exercise recreation of a missing parent above the logged subtree.
        (mirror.root / _ANCHOR).unlink()
        (mirror.root / "Docs").rmdir()
        previous = os.umask(umask)
        try:
            result = session.engine.undo(trashed.run_id)
        finally:
            os.umask(previous)

    assert result.failed == [] and path.read_bytes() == b"original"
    assert S_IMODE(path.stat().st_mode) == 0o600
    for folder in (path.parent, path.parent.parent, mirror.root / "Docs"):
        assert S_IMODE(folder.stat().st_mode) == 0o700


def test_dashboard_refuses_undo_from_an_earlier_mirror_setup(tmp_path: Path) -> None:
    rel = "Docs/report"
    mirror = make_mirror(tmp_path, {_ANCHOR: b"anchor", rel: b"original"})
    path = mirror.root / rel
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        item = live_items(mirror.fake, mirror.target)[rel]
        path.unlink()
        trashed = session.engine.run(trigger="manual")
        assert trashed.trashed == 1 and mirror.fake.file(item.id).trashed
        session.index.set_meta(META_UNDO_BOUNDARY, str(trashed.run_id))
        save_config(mirror.config, mirror.paths.config_file)

    def factory():
        opened = mirror.open()
        return opened.__enter__().engine, lambda: opened.__exit__(None, None, None)

    async def exercise_history() -> None:
        app = NimboApp(
            mirror.paths,
            language="en",
            reduced_motion=True,
            engine_factory=factory,
            service_state=lambda: ServiceState(True, True, False, None),
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("h")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, HistoryScreen)
            await pilot.press("enter")
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.click("#undo-run")
            assert isinstance(app.screen, UndoScreen)
            await pilot.click("#undo-confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert mirror.fake.file(item.id).trashed
            assert not path.exists()
            assert isinstance(app.screen, UndoScreen)
            status = app.screen.query_one("#undo-status", Static)
            assert "That run belongs to an earlier mirror setup." in status.content

    asyncio.run(exercise_history())
