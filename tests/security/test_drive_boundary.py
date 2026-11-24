"""Regression tests for Drive ownership and bounded child listings."""

from pathlib import Path

import httpx
import pytest

from nimbo.core.config import SafetyConfig
from nimbo.core.errors import DriveError
from nimbo.core.executor import _inherited_trash_children
from nimbo.core.model import OWNER_PROPERTY, PlanIssue, RunResult
from nimbo.core.safety import evaluate_brakes
from tests.system.helpers import make_mirror, put_file, write_calls

_OWNER = {OWNER_PROPERTY: "system-test-root"}


@pytest.mark.parametrize("folder", [False, True], ids=["file", "folder"])
def test_newer_editor_duplicate_cannot_replace_backup(tmp_path: Path, folder: bool) -> None:
    rel = "Docs/group/report" if folder else "Docs/report"
    mirror = make_mirror(tmp_path, {rel: b"original"})
    with mirror.open() as session:
        assert session.engine.run(trigger="manual").result is RunResult.OK
        docs = mirror.fake.find(mirror.target, "Docs")[0].id
        own = mirror.fake.find(docs, "group" if folder else "report")[0]
        if folder:
            other = mirror.fake.add_folder(
                "group",
                docs,
                properties=_OWNER,
                owned_by_me=False,
                modified_ms=own.modified_ms + 10_000,
            )
            own_file = mirror.fake.find(own.id, "report")[0].id
            other_file = mirror.fake.add_file("report", other, b"editor's file", owned_by_me=False)
        else:
            other = mirror.fake.add_file(
                "report",
                docs,
                b"editor's file",
                properties=_OWNER,
                owned_by_me=False,
                modified_ms=own.modified_ms + 10_000,
            )
            own_file, other_file = own.id, other
        protected = {file_id: mirror.fake.file(file_id) for file_id in {other, other_file}}
        put_file(mirror.root / rel, b"updated local content")
        start = len(mirror.fake.calls)

        result = session.engine.run(trigger="manual")

        assert not mirror.fake.file(own.id).trashed
        assert mirror.fake.file(own_file).content == b"updated local content"
        assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected
        assert not any(
            call.path.rsplit("/", 1)[-1] in protected for call in write_calls(mirror.fake, start)
        )
        assert result.result is RunResult.PARTIAL
        assert PlanIssue("foreign-item-kept", "Docs/group" if folder else rel) in result.warnings


def test_identical_editor_files_are_neither_adopted_nor_moved(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path, {"Docs/anchor": b"anchor"})
    with mirror.open() as session:
        assert session.engine.run(trigger="manual").result is RunResult.OK
        docs = mirror.fake.find(mirror.target, "Docs")[0].id
        new = mirror.root / "Docs/new"
        put_file(new, b"same bytes")
        put_file(mirror.root / "Docs/renamed", b"move candidate")
        at_path = mirror.fake.add_file(
            "new",
            docs,
            b"same bytes",
            properties=_OWNER,
            owned_by_me=False,
            modified_ms=new.stat().st_mtime_ns // 1_000_000,
        )
        movable = mirror.fake.add_file(
            "old",
            docs,
            b"move candidate",
            properties=_OWNER,
            owned_by_me=False,
        )
        protected = {file_id: mirror.fake.file(file_id) for file_id in (at_path, movable)}
        start = len(mirror.fake.calls)

        result = session.engine.run(trigger="manual")

        assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected
        assert not any(
            call.path.rsplit("/", 1)[-1] in protected for call in write_calls(mirror.fake, start)
        )
        assert result.result is RunResult.PARTIAL
        assert PlanIssue("foreign-item-kept", "Docs/new") in result.warnings
        assert result.adopted == result.moved == 0
        assert any(item.content == b"move candidate" for item in mirror.fake.find(docs, "renamed"))


def test_foreign_mirror_subtree_is_kept_while_other_paths_sync(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path, {"Docs/new": b"mirrored", "Docs/foreign/report": b"local"})
    docs = mirror.fake.add_folder("Docs", mirror.target)
    foreign = mirror.fake.add_folder("foreign", docs, properties={OWNER_PROPERTY: "another-root"})
    child = mirror.fake.add_file("report", foreign, b"other backup", properties=_OWNER)
    protected = {file_id: mirror.fake.file(file_id) for file_id in (foreign, child)}

    with mirror.open() as session:
        result = session.engine.run(trigger="manual")

    assert result.result is RunResult.PARTIAL
    assert PlanIssue("foreign-item-kept", "Docs/foreign") in result.warnings
    assert mirror.fake.find(docs, "new")[0].content == b"mirrored"
    assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected


def test_editor_duplicates_cannot_dilute_deletion_ratio(tmp_path: Path) -> None:
    deleted = {f"Docs/old/{number}": b"obsolete" for number in range(20)}
    mirror = make_mirror(tmp_path, {"Docs/anchor": b"anchor", **deleted})
    with mirror.open() as session:
        assert session.engine.run(trigger="manual").result is RunResult.OK
        docs = mirror.fake.find(mirror.target, "Docs")[0].id
        for rel in deleted:
            (mirror.root / rel).unlink()
        (mirror.root / "Docs/old").rmdir()
        safety = SafetyConfig(max_delete_ratio=0.5)
        before = session.engine.preview().plan
        assert evaluate_brakes(before, safety).reasons == ("folder_ratio:Docs",)
        for _ in range(30):
            mirror.fake.add_file("anchor", docs, b"duplicate", owned_by_me=False)

        after = session.engine.preview().plan

        assert evaluate_brakes(after, safety) == evaluate_brakes(before, safety)
        assert after.stats.per_folder_remote_files == {"Docs": 21}


@pytest.mark.parametrize("trash_audit", [False, True], ids=["children", "trash-audit"])
def test_repeated_listing_cursor_raises(tmp_path: Path, trash_audit: bool) -> None:
    mirror = make_mirror(tmp_path, {})
    with mirror.open() as session:
        pages = iter(["cursor-a", "cursor-b", "cursor-a"])

        def repeat_cursor(request: httpx.Request) -> httpx.Response:
            token = next(pages, None)
            assert token is not None, "listing requested another page after a repeated token"
            return httpx.Response(200, json={"nextPageToken": token, "files": []})

        session.drive._http._transport = httpx.MockTransport(repeat_cursor)
        listing = (
            _inherited_trash_children(session.drive, [mirror.target], lambda: False)
            if trash_audit
            else session.drive.list_children([mirror.target])
        )
        with pytest.raises(DriveError):
            list(listing)
