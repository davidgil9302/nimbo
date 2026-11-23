"""Tests for the shared Drive, planning, path and serialization contracts."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.model import (
    FOLDER_MIME,
    MAX_DRIVE_MS,
    SHORTCUT_MIME,
    About,
    ActiveTransfer,
    Op,
    OpKind,
    Plan,
    PlanIssue,
    PlanStats,
    Progress,
    RemoteFile,
    RunResult,
    RunSummary,
    drive_ms,
    is_under,
    ms_from_rfc3339,
    rel_depth,
    rel_join,
    rel_name,
    rel_parent,
    rel_top,
    rfc3339_from_ms,
)


@given(st.integers(min_value=0, max_value=MAX_DRIVE_MS))
def test_rfc3339_roundtrip(ms: int) -> None:
    assert ms_from_rfc3339(rfc3339_from_ms(ms)) == ms


@pytest.mark.parametrize(
    ("ms", "expected"),
    [(-1, "1970-01-01T00:00:00.000Z"), (MAX_DRIVE_MS + 1, "9999-12-31T23:59:59.999Z")],
)
def test_rfc3339_clamps_out_of_range(ms: int, expected: str) -> None:
    assert rfc3339_from_ms(ms) == expected


@pytest.mark.parametrize("suffix", ["Z", "+00:00", "+01:00", ""])
@pytest.mark.parametrize(
    ("fraction", "ms"),
    [
        ("", 0),
        (".1", 100),
        (".12", 120),
        (".123", 123),
        (".1234", 123),
        (".12345", 123),
        (".123456", 123),
    ],
)
def test_ms_from_rfc3339_variants(suffix: str, fraction: str, ms: int) -> None:
    hour = "01" if suffix == "+01:00" else "00"
    assert ms_from_rfc3339(f"1970-01-01T{hour}:00:00{fraction}{suffix}") == ms


@pytest.mark.parametrize(
    ("ns", "ms"),
    [
        (-1, 0),
        (0, 0),
        (999_999, 0),
        (1_999_999, 1),
        (MAX_DRIVE_MS * 1_000_000, MAX_DRIVE_MS),
        (10**30, MAX_DRIVE_MS),
    ],
)
def test_drive_ms_floors_and_clamps(ns: int, ms: int) -> None:
    assert drive_ms(ns) == ms


@pytest.mark.parametrize(
    ("rel", "parent", "name", "top", "depth"),
    [
        ("", "", "", "", 0),
        ("Projects", "", "Projects", "Projects", 1),
        ("Projects/notes/a", "Projects/notes", "a", "Projects", 3),
    ],
)
def test_rel_helpers(rel: str, parent: str, name: str, top: str, depth: int) -> None:
    assert rel_parent(rel) == parent
    assert rel_name(rel) == name
    assert rel_top(rel) == top
    assert rel_depth(rel) == depth
    assert rel_join(parent, name) == rel


@pytest.mark.parametrize(
    ("rel", "ancestor", "expected"),
    [
        ("", "", True),
        ("Projects", "", True),
        ("", "Projects", False),
        ("Projects", "Projects", True),
        ("Projects/a", "Projects", True),
        ("Projects2/a", "Projects", False),
        ("Projects", "Projects/a", False),
    ],
)
def test_is_under(rel: str, ancestor: str, expected: bool) -> None:
    assert is_under(rel, ancestor) is expected


def test_remote_from_api_parses_strings_and_missing_fields() -> None:
    assert RemoteFile.from_api({"id": "item"}) == RemoteFile("item", "", "")
    resource = {
        "id": "item",
        "name": "notes.txt",
        "mimeType": "text/plain",
        "parents": ["parent"],
        "size": "123456789012345",
        "md5Checksum": "digest",
        "modifiedTime": "1970-01-01T00:00:01.123456Z",
        "properties": {"nimbo": "source-root", "other": 42},
        "trashed": True,
    }
    entry = RemoteFile.from_api(resource)
    assert entry == RemoteFile(
        "item",
        "notes.txt",
        "text/plain",
        ("parent",),
        123456789012345,
        "digest",
        1123,
        {"nimbo": "source-root", "other": "42"},
        True,
    )
    assert entry.owner_root == "source-root"


@pytest.mark.parametrize(
    ("mime", "folder", "shortcut", "native"),
    [
        (FOLDER_MIME, True, False, False),
        (SHORTCUT_MIME, False, True, True),
        ("application/vnd.google-apps.document", False, False, True),
        ("application/pdf", False, False, False),
    ],
)
def test_google_native_vs_folder_vs_shortcut(
    mime: str, folder: bool, shortcut: bool, native: bool
) -> None:
    entry = RemoteFile("item", "entry", mime)
    assert entry.is_folder is folder
    assert entry.is_shortcut is shortcut
    assert entry.is_google_native is native


def test_plan_fingerprint_ignores_order_and_non_trash_ops() -> None:
    a = Op(OpKind.TRASH, "Projects/a", "deleted-locally", affected_files=1)
    b = Op(OpKind.TRASH, "Photos/b", "extraneous", affected_files=2)
    upload = Op(OpKind.UPLOAD, "Projects/new", "new")
    move = Op(OpKind.MOVE, "Photos/new", "renamed", src_rel="Photos/old")
    planned = Plan([a, b], PlanStats())
    reordered = Plan([move, b, a, upload], PlanStats())
    assert planned.fingerprint() == reordered.fingerprint()


@pytest.mark.parametrize(
    ("rel", "affected"),
    [("Projects/new", 1), ("Projects/a", 2)],
)
def test_plan_fingerprint_changes_with_trash_set(rel: str, affected: int) -> None:
    a = Op(OpKind.TRASH, "Projects/a", "deleted-locally", affected_files=1)
    b = Op(OpKind.TRASH, rel, "deleted-locally", affected_files=affected)
    assert Plan([a], PlanStats()).fingerprint() != Plan([b], PlanStats()).fingerprint()
    assert Plan([a], PlanStats()).fingerprint() != Plan([a, b], PlanStats()).fingerprint()
    assert Plan([a], PlanStats()).fingerprint() != Plan([], PlanStats()).fingerprint()


@pytest.mark.parametrize("result", list(RunResult))
def test_run_summary_roundtrip(result: RunResult) -> None:
    summary = RunSummary(
        17,
        result,
        uploaded=2,
        uploaded_bytes=400,
        failed=[("Projects/f", "network_failed")],
        warnings=[PlanIssue("google-native-kept", "Projects/é.doc", "untouched")],
        fingerprint="fingerprint",
        trash_preview=["Projects/a\t2"],
    )
    data = json.loads(json.dumps(summary.to_dict()))
    assert data["result"] == result.value
    assert RunSummary.from_dict(data) == summary
    minimal = {"run_id": None, "result": result.value}
    assert RunSummary.from_dict(minimal) == RunSummary(None, result)


def test_progress_json_roundtrip() -> None:
    stamp = "2026-01-01T00:00:00Z"
    progress = Progress(
        17,
        123,
        "transfer",
        stamp,
        stamp,
        folder="Projets été",
        ops_total=4,
        ops_done=1,
        bytes_total=80,
        bytes_done=20,
        speed_bps=4.5,
        eta_s=13,
        active=[ActiveTransfer("Projets été/é.txt", 20, 80)],
        speed_history=[3.1, 4.5],
    )
    serialized = progress.to_json()
    assert "Projets été" in serialized
    assert Progress.from_json(serialized) == progress
    minimal = {
        "run_id": 17,
        "pid": 123,
        "phase": "preflight",
        "started_at": stamp,
        "updated_at": stamp,
    }
    assert Progress.from_json(json.dumps(minimal)) == Progress(17, 123, "preflight", stamp, stamp)


@pytest.mark.parametrize(
    ("limit", "usage", "free"), [(None, 100, None), (100, 25, 75), (100, 100, 0), (10, 20, 0)]
)
def test_about_free(limit: int | None, usage: int, free: int | None) -> None:
    assert About(None, limit, usage, 4).free == free


def test_plan_fingerprint_unambiguous_with_tabs_and_newlines() -> None:
    """Names may contain tabs/newlines; distinct deletion sets must never share a fingerprint."""
    from nimbo.core.model import Op, OpKind, Plan, PlanStats

    def trash(rel: str, affected: int) -> Op:
        return Op(kind=OpKind.TRASH, rel=rel, reason="deleted-locally", affected_files=affected)

    tricky = Plan(ops=[trash("Projects/a\t1\nProjects/b", 2)], stats=PlanStats())
    plain = Plan(ops=[trash("Projects/a", 1), trash("Projects/b", 2)], stats=PlanStats())
    assert tricky.fingerprint() != plain.fingerprint()


def test_local_entry_disk_rel_defaults_to_rel() -> None:
    from nimbo.core.model import LocalEntry

    assert LocalEntry("DOC/a.txt", False, 1, 2).disk_rel == "DOC/a.txt"
    nfd = "DOC/Más.txt"
    assert LocalEntry("DOC/Más.txt", False, 1, 2, disk=nfd).disk_rel == nfd


def test_plan_fingerprint_binds_drive_ids() -> None:
    """An approval for one Drive object must not authorize trashing another one at the same path."""
    from nimbo.core.model import Op, OpKind, Plan, PlanStats

    def plan_for(file_id: str) -> Plan:
        op = Op(
            kind=OpKind.TRASH,
            rel="Projects/a",
            reason="deleted-locally",
            file_id=file_id,
            affected_files=1,
        )
        return Plan(ops=[op], stats=PlanStats())

    assert plan_for("id-1").fingerprint() != plan_for("id-2").fingerprint()
    assert plan_for("id-1").fingerprint() == plan_for("id-1").fingerprint()
