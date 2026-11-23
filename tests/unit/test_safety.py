from __future__ import annotations

import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nimbo.core import safety
from nimbo.core.config import Config, OAuthConfig, SafetyConfig, SourceConfig, TargetConfig
from nimbo.core.errors import SafetyError
from nimbo.core.fsutil import read_json, write_json
from nimbo.core.model import NOBACKUP_NAME, SENTINEL_NAME, About, Op, OpKind, Plan, PlanStats

_NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


def _config(root: Path, *, require_mount: bool = False) -> Config:
    return Config(
        SourceConfig(
            root, ("Projects", "Paused", "Photos", "Excluded"), ("Paused",), require_mount
        ),
        TargetConfig("example-folder"),
        OAuthConfig("example-client", "example-secret"),
    )


def _populate(root: Path) -> None:
    for folder in ("Projects", "Photos", "Excluded"):
        path = root / folder
        path.mkdir()
        (path / (NOBACKUP_NAME if folder == "Excluded" else "file.txt")).touch()
    safety.write_sentinel(root, "root-id")


def test_sentinel_roundtrip(source_root: Path) -> None:
    safety.write_sentinel(source_root, "root-id")
    assert safety.read_sentinel(source_root) == "root-id"
    marker = source_root / SENTINEL_NAME
    data = read_json(marker)
    assert data == {"app": "nimbo", "uuid": "root-id", "created": data["created"]}
    assert datetime.fromisoformat(data["created"]).tzinfo is not None
    original = marker.stat()
    safety.write_sentinel(source_root, "root-id")
    assert marker.stat().st_ino == original.st_ino
    assert marker.stat().st_mtime_ns == original.st_mtime_ns


@pytest.mark.parametrize(
    "contents",
    [
        None,
        b"{broken",
        b"\xff",
        b"[]",
        b'{"app":"foreign","uuid":"root-id"}',
        b'{"app":"nimbo","uuid":43}',
        b'{"app":"nimbo","uuid":""}',
    ],
)
def test_sentinel_corrupt_is_none(source_root: Path, contents: bytes | None) -> None:
    if contents is not None:
        (source_root / SENTINEL_NAME).write_bytes(contents)
    assert safety.read_sentinel(source_root) is None


def test_write_sentinel_refuses_other_uuid(source_root: Path) -> None:
    safety.write_sentinel(source_root, "first")
    with pytest.raises(SafetyError) as exc:
        safety.write_sentinel(source_root, "second")
    assert exc.value.code == "sentinel_mismatch"
    assert safety.read_sentinel(source_root) == "first"


def test_write_sentinel_concurrent_initialization(source_root: Path) -> None:
    def initialize(root_uuid: str) -> str:
        try:
            safety.write_sentinel(source_root, root_uuid)
        except SafetyError as error:
            assert error.code == "sentinel_mismatch"
            return ""
        return root_uuid

    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = list(pool.map(initialize, ("first", "second")))
    assert [value for value in writes if value] == [safety.read_sentinel(source_root)]


def test_write_sentinel_preserves_unknown_file(source_root: Path) -> None:
    marker = source_root / SENTINEL_NAME
    marker.write_text("unknown", encoding="utf-8")
    with pytest.raises(SafetyError) as exc:
        safety.write_sentinel(source_root, "root-id")
    assert exc.value.code == "sentinel_mismatch"
    assert marker.read_text(encoding="utf-8") == "unknown"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("healthy", []),
        ("mounted", []),
        ("unrecorded", []),
        ("mount", [("source_not_mounted", "")]),
        (
            "root",
            [
                ("source_missing", ""),
                ("sentinel_missing", ""),
                ("folder_missing", "Projects"),
                ("folder_missing", "Photos"),
                ("folder_missing", "Excluded"),
            ],
        ),
        ("sentinel", [("sentinel_missing", "")]),
        ("uuid", [("sentinel_mismatch", "")]),
        ("missing", [("folder_missing", "Projects")]),
        ("file", [("folder_missing", "Projects")]),
        ("symlink", [("folder_missing", "Projects")]),
        ("empty", [("folder_empty", "Projects")]),
        ("unreadable", [("folder_unreadable", "Projects")]),
        (
            "combined",
            [
                ("source_not_mounted", ""),
                ("sentinel_mismatch", ""),
                ("folder_empty", "Projects"),
                ("folder_missing", "Photos"),
            ],
        ),
    ],
)
def test_check_source_all_codes(
    source_root: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: list[tuple[str, str]]
) -> None:
    root = source_root / "Missing" if case == "root" else source_root
    if root == source_root:
        _populate(root)
    projects = root / "Projects"
    if case in {"missing", "file", "symlink", "empty", "combined"}:
        (projects / "file.txt").unlink()
    if case in {"missing", "file", "symlink"}:
        projects.rmdir()
    if case == "file":
        projects.touch()
    if case == "symlink":
        projects.symlink_to(root / "Photos", target_is_directory=True)
    if case == "sentinel":
        (root / SENTINEL_NAME).unlink()
    if case == "combined":
        (root / "Photos" / "file.txt").unlink()
        (root / "Photos").rmdir()
    if case == "unreadable":
        original = os.scandir

        def scandir(path: Path):
            if path == projects:
                raise PermissionError(path)
            return original(path)

        monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os.path, "ismount", lambda path: case == "mounted" and path == root)
    cfg = _config(root, require_mount=case in {"mounted", "mount", "combined"})
    expected_uuid = None if case == "unrecorded" else "root-id"
    if case in {"uuid", "combined"}:
        expected_uuid = "other-id"
    problems = safety.check_source(cfg, expected_uuid)
    assert [(problem.code, problem.detail) for problem in problems] == expected


def test_source_mount_error_still_reports_other_problems(
    source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_mount(path: Path) -> bool:
        raise PermissionError(path)

    monkeypatch.setattr(os.path, "ismount", bad_mount)
    codes = [
        problem.code
        for problem in safety.check_source(_config(source_root, require_mount=True), "root-id")
    ]
    assert codes == ["source_not_mounted", "sentinel_missing", *["folder_missing"] * 3]


@pytest.mark.parametrize("error", [None, TimeoutError(), socket.gaierror()])
def test_network_available_uses_timeout(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None
) -> None:
    calls = []
    closed = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *exc: object) -> None:
            closed.append(True)

    def connect(address: tuple[str, int], timeout: float) -> Connection:
        calls.append((address, timeout))
        if error:
            raise error
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    assert safety.network_available("example.test", 444, 1.25) is (error is None)
    assert calls == [(("example.test", 444), 1.25)]
    assert closed == ([True] if error is None else [])


@pytest.mark.parametrize(("count", "tripped"), [(5, False), (6, True)])
def test_brakes_total(count: int, tripped: bool) -> None:
    plan = Plan(
        [
            Op(
                OpKind.TRASH,
                "Projects/item",
                "deleted-locally",
                file_id="item-id",
                affected_files=count,
            )
        ],
        PlanStats(trashed_files=count),
    )
    decision = safety.evaluate_brakes(plan, SafetyConfig(max_deletes=5))
    assert decision.tripped is tripped
    assert decision.reasons == (("too_many_deletes",) if tripped else ())
    assert decision.fingerprint == plan.fingerprint()


def test_brakes_ratio_with_floor() -> None:
    stats = PlanStats(
        trashed_files=63,
        per_folder_trashed={"Z": 21, "BelowFloor": 1, "Equal": 20, "A": 21},
        per_folder_remote_files={"Z": 0, "Equal": 100},
    )
    decision = safety.evaluate_brakes(
        Plan([], stats), SafetyConfig(max_deletes=50, max_delete_ratio=0.2, ratio_min_files=20)
    )
    assert decision.tripped
    assert decision.reasons == ("folder_ratio:A", "folder_ratio:Z", "too_many_deletes")


def test_brakes_not_tripped() -> None:
    plan = Plan([], PlanStats(trashes=1, per_folder_trashed={"Projects": 0}))
    assert safety.evaluate_brakes(plan, SafetyConfig(0, 0.1, 0)).reasons == ()
    assert not safety.evaluate_brakes(plan, SafetyConfig(0, 0.1, 0)).tripped


def test_brakes_ratio_uses_folder_count_independently() -> None:
    plan = Plan([], PlanStats(per_folder_trashed={"Projects": 20}))
    assert safety.evaluate_brakes(plan, SafetyConfig()).reasons == ("folder_ratio:Projects",)


@pytest.mark.parametrize(
    ("limit", "usage", "problem"),
    [(None, 99, False), (100, 10, False), (100, 11, True), (50, 99, True)],
)
def test_check_space(limit: int | None, usage: int, problem: bool) -> None:
    result = safety.check_space(
        Plan([], PlanStats(upload_bytes=80, update_bytes=10)), About(None, limit, usage, 0)
    )
    assert (result.code if result else None) == ("storage_insufficient" if problem else None)


def test_approval_match_expiry_and_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "state" / "approval.json"
    assert not safety.consume_approval(path, "plan", now=_NOW)
    safety.write_approval(path, "plan", now=_NOW)
    assert read_json(path) == {
        "fingerprint": "plan",
        "expires": (_NOW + timedelta(days=1)).isoformat(),
    }
    assert safety.consume_approval(path, "plan", now=_NOW + timedelta(hours=23))
    assert not path.exists()
    assert not safety.consume_approval(path, "plan", now=_NOW)

    safety.write_approval(path, "plan", now=_NOW, ttl=timedelta(seconds=1))
    assert not safety.consume_approval(path, "plan", now=_NOW + timedelta(seconds=1))
    assert not path.exists()

    safety.write_approval(path, "plan", now=_NOW)
    assert not safety.consume_approval(path, "different", now=_NOW)
    assert not path.exists()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"fingerprint": "plan", "expires": 3},
        {"fingerprint": "plan", "expires": "invalid"},
        {"fingerprint": "plan", "expires": "9999-01-01T00:00:00"},
    ],
)
def test_corrupt_approval_deleted(tmp_path: Path, data: object) -> None:
    path = tmp_path / "approval.json"
    write_json(path, data)
    assert not safety.consume_approval(path, "plan", now=_NOW)
    assert not path.exists()


def test_approval_requires_successful_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "approval.json"
    safety.write_approval(path, "plan", now=_NOW)

    def deny_unlink(path: Path) -> None:
        raise PermissionError(path)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    assert not safety.consume_approval(path, "plan", now=_NOW)
    assert path.exists()


def test_approval_can_only_be_consumed_once(tmp_path: Path) -> None:
    path = tmp_path / "approval.json"
    safety.write_approval(path, "plan", now=_NOW)
    child_code = """\
import sys
from datetime import datetime
from pathlib import Path
from nimbo.core.safety import consume_approval

approved = consume_approval(Path(sys.argv[1]), "plan", now=datetime.fromisoformat(sys.argv[2]))
sys.exit(0 if approved else 1)
"""

    def consume(_: int) -> bool:
        completed = subprocess.run(
            [sys.executable, "-c", child_code, str(path), _NOW.isoformat()], timeout=5, check=False
        )
        assert completed.returncode in (0, 1)
        return completed.returncode == 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(consume, range(5))) == 1
