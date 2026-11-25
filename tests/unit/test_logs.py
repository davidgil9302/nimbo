from __future__ import annotations

import json
import logging
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from nimbo.core import logs
from nimbo.core.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[Paths]:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        yield Paths(tmp_path / "config", tmp_path / "state")
    finally:
        for handler in root.handlers[:]:
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original_level)


def test_jsonl_written_and_rotated(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.log_dir.mkdir(parents=True)
    now = "2026-11-23T23:59:59+00:00"
    monkeypatch.setattr(logs, "utc_now_iso", lambda: now)
    old = paths.log_dir / "nimbo-2026-10-23.jsonl"
    boundary = paths.log_dir / "nimbo-2026-10-24.jsonl"
    unrelated = paths.log_dir / "example-2026-10-23.jsonl"
    for path in (old, boundary, unrelated):
        path.write_text("{}\n")
    logger = logging.getLogger("nimbo.tests")
    logs.setup_logging(paths)
    logs.setup_logging(paths, verbose=True)
    logger.debug("transfer %s", "started", extra={"file_count": 2})
    now = "2026-11-24T00:00:00+00:00"
    logger.info("transfer completed")

    assert not old.exists()
    assert boundary.exists()
    assert unrelated.exists()
    yesterday = paths.log_dir / "nimbo-2026-11-23.jsonl"
    assert [json.loads(line) for line in yesterday.read_text().splitlines()] == [
        {
            "ts": "2026-11-23T23:59:59+00:00",
            "level": "DEBUG",
            "logger": "nimbo.tests",
            "msg": "transfer started",
            "file_count": 2,
        }
    ]
    today = paths.log_dir / "nimbo-2026-11-24.jsonl"
    assert json.loads(today.read_text())["msg"] == "transfer completed"
    assert capsys.readouterr().err.splitlines() == [
        "DEBUG nimbo.tests transfer started",
        "INFO nimbo.tests transfer completed",
    ]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("failed ya29.example-token_123", "ya29.example-token_123"),
        ("failed 1//example_refresh-123", "1//example_refresh-123"),
        ("Authorization: Bearer example-TOKEN_123==", "example-TOKEN_123=="),
        ("https://example.test/?client_secret=arbitrary-secret&x=1", "arbitrary-secret"),
        ('{"refresh_token": "sample-refresh"}', "sample-refresh"),
        ('{"access_token":"opaque-access"}', "opaque-access"),
        ('{"access_token":"with\\"escaped-quote"}', "escaped-quote"),
        ("{'refresh_token': 'quoted refresh'}", "quoted refresh"),
        ("?upload_id=sample-upload&x=1", "sample-upload"),
        ("?code=sample-code%2Fdemo#fragment", "sample-code%2Fdemo"),
    ],
)
def test_redaction_patterns(
    paths: Paths, capsys: pytest.CaptureFixture[str], text: str, secret: str
) -> None:
    record = logging.LogRecord(
        "nimbo.tests", logging.INFO, "example.py", 1, "text %s", (text,), None
    )
    assert logs.RedactingFilter().filter(record)
    assert secret not in record.getMessage()
    assert "REDACTED" in record.getMessage()

    logs.setup_logging(paths)
    logging.getLogger("nimbo.tests").info("text %s", text, extra={"nested": {"details": [text]}})
    (logged,) = logs.read_log_tail(paths)
    output = json.dumps(logged)
    assert secret not in output
    assert "REDACTED" in logged["msg"]
    assert "REDACTED" in logged["nested"]["details"][0]
    assert secret not in capsys.readouterr().err


def test_structured_secrets_and_exception_are_redacted(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    class Secret:
        def __str__(self) -> str:
            return "client_secret=object-secret"

    logs.setup_logging(paths)
    try:
        raise ValueError("Bearer exception-secret")
    except ValueError:
        logging.getLogger("nimbo.tests").exception(
            "connection error",
            stack_info=True,
            extra={
                "access_token": "arbitrary-secret",
                "nested": {"refresh_token": "other-secret", "detail": Secret()},
                "level": "spoofed",
            },
        )
    (logged,) = logs.read_log_tail(paths)
    assert logged["level"] == "ERROR"
    assert logged["access_token"] == "[REDACTED]"
    assert logged["nested"]["refresh_token"] == "[REDACTED]"
    assert logged["nested"]["detail"] == "client_secret=[REDACTED]"
    assert "ValueError: Bearer [REDACTED]" in logged["exception"]
    assert "Stack (most recent call last)" in logged["stack"]
    assert "exception-secret" not in capsys.readouterr().err


def test_read_log_tail(paths: Paths) -> None:
    assert logs.read_log_tail(paths) == []
    paths.log_dir.mkdir(parents=True)
    (paths.log_dir / "nimbo-2026-11-21.jsonl").write_bytes(b'{"msg":"one"}\n')
    (paths.log_dir / "nimbo-2026-11-22.jsonl").write_bytes(b'{"msg":"two"}\n{"msg":"three"}\n')
    (paths.log_dir / "nimbo-2026-11-23.jsonl").write_bytes(
        b'invalid\n\xff\n[]\n{"msg":"four"}\n{"truncated"'
    )
    (paths.log_dir / "nimbo-9999-99-99.jsonl").write_bytes(b'{"msg":"invalid date"}\n')
    (paths.log_dir / "example.jsonl").write_bytes(b'{"msg":"ignore"}\n')
    assert logs.read_log_tail(paths, lines=3) == [{"msg": "two"}, {"msg": "three"}, {"msg": "four"}]
    assert logs.read_log_tail(paths, lines=1) == [{"msg": "four"}]
    assert logs.read_log_tail(paths, lines=0) == []


def test_logging_refuses_symlink_destination(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs, "utc_now_iso", lambda: "2026-11-23T00:00:00Z")
    logs.setup_logging(paths, stderr=False)
    outside = tmp_path / "unrelated.log"
    outside.write_text("original\n")
    (paths.log_dir / "nimbo-2026-11-23.jsonl").symlink_to(outside)
    logging.getLogger("nimbo.tests").warning("skip the symlink")
    assert outside.read_text() == "original\n"
    assert logs.read_log_tail(paths) == []


def test_logging_failure_is_silent_and_stderr_is_optional(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.state_dir.mkdir()
    paths.log_dir.write_text("cannot be opened as a directory")
    logs.setup_logging(paths)
    logger = logging.getLogger("nimbo.tests")
    logger.warning("client_secret=%s", "placeholder")
    assert capsys.readouterr().err == "WARNING nimbo.tests client_secret=[REDACTED]\n"
    logs.setup_logging(paths, stderr=False)
    # Exercise only Nimbo's sink, since pytest also installs a handler on the root.
    handler = logging.getLogger().handlers[-1]
    handler.handle(
        logger.makeRecord(
            logger.name, logging.ERROR, __file__, 1, "bad format %s %s", ("one",), None
        )
    )
    logger.info("well formed")
    assert capsys.readouterr().err == ""
    assert logs.read_log_tail(paths) == []


def test_new_and_existing_logs_are_private(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs, "utc_now_iso", lambda: "2026-11-23T20:00:00+00:00")
    paths.log_dir.mkdir(parents=True)
    old = paths.log_dir / "nimbo-2026-11-22.jsonl"
    old.write_text("{}", encoding="utf-8")
    old.chmod(0o644)

    logs.setup_logging(paths, stderr=False)
    logger = logging.getLogger("nimbo.tests")
    logger.warning("first")
    latest = paths.log_dir / "nimbo-2026-11-23.jsonl"
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    latest.chmod(0o644)
    logger.warning("second")

    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
