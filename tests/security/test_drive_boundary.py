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
        assert result.result is RunResult.OK
        assert PlanIssue("foreign-item-kept", "Docs/group" if folder else rel) in result.warnings
        start = len(mirror.fake.calls)
        assert session.engine.run(trigger="manual").result is RunResult.OK
        assert not write_calls(mirror.fake, start)


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
        assert result.result is RunResult.OK
        assert PlanIssue("foreign-item-kept", "Docs/new") in result.warnings
        assert result.adopted == result.moved == 0
        assert result.uploaded == 2
        (backup,) = (item for item in mirror.fake.find(docs, "new") if item.id != at_path)
        assert backup.properties == _OWNER and backup.content == b"same bytes"
        assert any(item.content == b"move candidate" for item in mirror.fake.find(docs, "renamed"))
        start = len(mirror.fake.calls)
        second = session.engine.run(trigger="manual")
        assert second.result is RunResult.OK
        assert PlanIssue("foreign-item-kept", "Docs/new") in second.warnings
        assert not write_calls(mirror.fake, start)
        assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected


@pytest.mark.parametrize("kind", ["folder", "document"])
def test_local_subtree_syncs_alongside_an_untouchable_item(tmp_path: Path, kind: str) -> None:
    mirror = make_mirror(tmp_path, {"Docs/new": b"mirrored", "Docs/foreign/report": b"local"})
    docs = mirror.fake.add_folder("Docs", mirror.target)
    if kind == "folder":
        foreign = mirror.fake.add_folder(
            "foreign", docs, properties={OWNER_PROPERTY: "another-root"}
        )
        child = mirror.fake.add_file("report", foreign, b"other backup", properties=_OWNER)
        protected_ids = (foreign, child)
        notice = "foreign-item-kept"
    else:
        foreign = mirror.fake.add_native("foreign", docs)
        protected_ids = (foreign,)
        notice = "google-native-kept"
    protected = {file_id: mirror.fake.file(file_id) for file_id in protected_ids}

    with mirror.open() as session:
        result = session.engine.run(trigger="manual")
        assert result.result is RunResult.OK
        assert PlanIssue(notice, "Docs/foreign") in result.warnings
        assert session.index.folder_stats()["Docs"].last_ok == result.finished_at
        assert mirror.fake.find(docs, "new")[0].content == b"mirrored"
        (backup,) = (item for item in mirror.fake.find(docs, "foreign") if item.id != foreign)
        assert backup.is_folder and backup.properties == _OWNER
        (report,) = mirror.fake.find(backup.id, "report")
        assert report.content == b"local"
        assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected

    start = len(mirror.fake.calls)
    with mirror.open() as session:
        second = session.engine.run(trigger="manual")
    assert second.result is RunResult.OK
    assert PlanIssue(notice, "Docs/foreign") in second.warnings
    assert not write_calls(mirror.fake, start)
    assert {file_id: mirror.fake.file(file_id) for file_id in protected} == protected


@pytest.mark.parametrize("local_dir", [False, True], ids=["local-file", "local-folder"])
def test_local_type_change_with_foreign_homonym(tmp_path: Path, local_dir: bool) -> None:
    rel = "Docs/shared"
    local_file = f"{rel}/report" if local_dir else rel
    mirror = make_mirror(tmp_path, {local_file: b"local"})
    docs = mirror.fake.add_folder("Docs", mirror.target)
    if local_dir:
        obsolete = mirror.fake.add_file("shared", docs, b"old file", properties=_OWNER)
        foreign = mirror.fake.add_folder(
            "shared", docs, properties={OWNER_PROPERTY: "another-root"}
        )
    else:
        obsolete = mirror.fake.add_folder("shared", docs, properties=_OWNER)
        foreign = mirror.fake.add_file("shared", docs, b"editor's bytes", owned_by_me=False)
    preserved = mirror.fake.file(foreign)

    with mirror.open() as session:
        result = session.engine.run(trigger="manual")
        assert result.result is RunResult.OK
        assert result.trashed == 1 and result.uploaded == 1
        assert PlanIssue("foreign-item-kept", rel) in result.warnings
        assert mirror.fake.file(obsolete).trashed
        (backup,) = (
            item for item in mirror.fake.find(docs, "shared") if item.id not in {obsolete, foreign}
        )
        assert backup.properties == _OWNER and backup.is_folder == local_dir
        (payload,) = mirror.fake.find(backup.id, "report") if local_dir else (backup,)
        assert payload.content == b"local"
        start = len(mirror.fake.calls)
        assert session.engine.run(trigger="manual").result is RunResult.OK
        assert not write_calls(mirror.fake, start)
    assert mirror.fake.file(foreign) == preserved


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
