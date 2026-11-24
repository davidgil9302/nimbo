"""Regression tests for overwrite approvals and the journal's account binding."""

import os
from pathlib import Path

import pytest

from nimbo.app import cli
from nimbo.core.config import save_config
from nimbo.core.index import META_ACCOUNT
from nimbo.core.model import RunResult
from nimbo.core.safety import write_approval
from tests.system.helpers import FIXED_TIME, Mirror, assert_ok, count_writes, make_mirror, put_file

_FILES = tuple(f"Docs/subfolder/file-{i}.txt" for i in range(60))


@pytest.fixture
def mirror(tmp_path: Path) -> Mirror:
    mirror = make_mirror(tmp_path, dict.fromkeys(_FILES, b"before"))
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="timer"))
        assert session.index.get_meta(META_ACCOUNT) == "drive-user@example.test"
    mirror.assert_state()
    return mirror


def _assert_held_without_writes(mirror: Mirror) -> str:
    before = mirror.fake.snapshot(mirror.target)
    start = len(mirror.fake.calls)
    with mirror.open() as session:
        summary = session.engine.run(trigger="timer")
    assert summary.result is RunResult.NEEDS_CONFIRMATION
    assert summary.fingerprint is not None
    assert count_writes(mirror.fake, start) == 0
    assert mirror.fake.snapshot(mirror.target) == before
    return summary.fingerprint


def test_mass_overwrite_waits_for_approval_then_uploads(mirror: Mirror) -> None:
    for rel in _FILES:
        put_file(mirror.root / rel, b"after!")

    fingerprint = _assert_held_without_writes(mirror)
    write_approval(mirror.paths.approval_file, fingerprint, now=FIXED_TIME)

    with mirror.open() as session:
        summary = session.engine.run(trigger="timer")
    assert_ok(summary)
    assert summary.updated == 60
    assert not mirror.paths.approval_file.exists()
    mirror.assert_state()


def test_approval_for_different_overwrites_is_rejected(mirror: Mirror) -> None:
    for rel in _FILES:
        put_file(mirror.root / rel, b"first rewrite")
    fingerprint = _assert_held_without_writes(mirror)
    write_approval(mirror.paths.approval_file, fingerprint, now=FIXED_TIME)

    path = mirror.root / _FILES[0]
    previous = path.stat()
    path.write_bytes(b"other rewrite")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))

    assert _assert_held_without_writes(mirror) != fingerprint
    assert not mirror.paths.approval_file.exists()


def test_handful_of_edits_runs_unattended(mirror: Mirror) -> None:
    for rel in _FILES[:4]:
        put_file(mirror.root / rel, b"edited")
    with mirror.open() as session:
        summary = session.engine.run(trigger="timer")
    assert_ok(summary)
    assert summary.updated == 4
    mirror.assert_state()


def test_cli_reviews_overwrites_and_runs_approved_plan(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for rel in _FILES:
        put_file(mirror.root / rel, b"after!")
    save_config(mirror.config, mirror.paths.config_file)
    monkeypatch.setenv("NIMBO_CONFIG_DIR", str(mirror.paths.config_dir))
    monkeypatch.setenv("NIMBO_STATE_DIR", str(mirror.paths.state_dir))

    with mirror.open() as session:
        monkeypatch.setattr(cli, "build_engine", lambda *_args: (session.engine, lambda: None))
        assert cli.main(["--lang", "en", "now", "--here"]) == 4
        assert "overwrite files in Drive" in capsys.readouterr().out
        assert cli.main(["--lang", "en", "now", "--here", "--confirm", "--yes"]) == 0
    output = capsys.readouterr().out
    assert "Overwrite" in output and _FILES[0] in output and _FILES[-1] in output
    assert not mirror.paths.approval_file.exists()
    mirror.assert_state()


def test_different_google_account_stops_before_any_drive_write(mirror: Mirror) -> None:
    recorded = "original-account@example.test"
    put_file(mirror.root / _FILES[0], b"edited")
    start = len(mirror.fake.calls)
    with mirror.open() as session:
        # FakeDrive identifies the active credential as drive-user@example.test.
        session.index.set_meta(META_ACCOUNT, recorded)
        summary = session.engine.run(trigger="timer")
        assert summary.result is RunResult.FAILED
        assert summary.reason == "account_changed"
        assert session.index.get_meta(META_ACCOUNT) == recorded
    assert count_writes(mirror.fake, start) == 0
    assert [(call.method, call.path) for call in mirror.fake.calls[start:]] == [
        ("GET", "/drive/v3/about")
    ]
