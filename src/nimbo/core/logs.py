"""Write and read daily JSONL logs with credential redaction."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import sys
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from copy import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TextIO

from nimbo.core.model import utc_now_iso
from nimbo.core.paths import Paths

_REDACTED = "[REDACTED]"
_SECRET_KEYS = ("access_token", "refresh_token", "client_secret", "upload_id", "code")
_SECRET_KEY_PATTERN = "|".join(_SECRET_KEYS)
_SECRET_FIELDS = re.compile(
    rf"""(?P<prefix>(?<![\w-])["']?(?:{_SECRET_KEY_PATTERN})["']?\s*[:=]\s*)"""
    r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s&#"',;}\]]+)""",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bBearer[ \t]+)[A-Za-z0-9._~+/%=-]+", re.IGNORECASE)
_TOKENS = re.compile(r"(?:ya29\.|1//)[A-Za-z0-9._~+/%=-]+")
_LOG_NAME = re.compile(r"nimbo-(\d{4}-\d{2}-\d{2})\.jsonl")
_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
}
_JSON_FIELDS = frozenset({"ts", "level", "logger", "msg", "exception", "stack"})
_TRACEBACK_FORMATTER = logging.Formatter()
_APPEND = os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_INSPECT = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _private_log(fd: int) -> bool:
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        return False
    if stat.S_IMODE(mode) != 0o600:
        os.fchmod(fd, 0o600)
    return True


def _replace_field(match: re.Match[str]) -> str:
    value = match["value"]
    quote = value[0] if value[0] in {"'", '"'} else ""
    return match["prefix"] + quote + _REDACTED + quote


def _redact(text: str) -> str:
    text = _SECRET_FIELDS.sub(_replace_field, text)
    text = _BEARER.sub(r"\g<1>" + _REDACTED, text)
    return _TOKENS.sub(_REDACTED, text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(_redact_entry(str(key), item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if (
        value is None
        or isinstance(value, (bool, int))
        or (isinstance(value, float) and math.isfinite(value))
    ):
        return value
    return _redact(str(value))


def _redact_entry(key: str, value: Any) -> tuple[str, Any]:
    return _redact(key), _REDACTED if key.casefold() in _SECRET_KEYS else _redact_value(value)


class RedactingFilter(logging.Filter):
    """Redact formatted records and recursively sanitize structured fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitize a record in place, returning False if it cannot be safely formatted."""
        try:
            record.msg = _redact(record.getMessage())
            record.message = record.msg
            record.args = ()
            record.name = _redact(record.name)
            record.levelname = _redact(record.levelname)
            if record.exc_info is not None:
                record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
                record.exc_info = None
            if record.exc_text:
                record.exc_text = _redact(record.exc_text)
            if record.stack_info:
                record.stack_info = _redact(record.stack_info)
            for key in record.__dict__.keys() - _STANDARD_FIELDS:
                clean_key, value = _redact_entry(key, record.__dict__.pop(key))
                record.__dict__[clean_key] = value
        except Exception:  # noqa: BLE001 - logging must drop malformed records safely.
            return False
        return True


class _LogHandler(logging.Handler):
    def __init__(self, directory: Path, level: int, stderr: TextIO | None) -> None:
        super().__init__(level)
        self._directory = directory
        self._stderr = stderr
        self._redactor = RedactingFilter()
        self._human = logging.Formatter("%(levelname)s %(name)s %(message)s")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            clean = copy(record)
        except Exception:  # noqa: BLE001 - custom record types can fail to copy.
            return
        if not self._redactor.filter(clean):
            return
        with suppress(Exception):
            timestamp = utc_now_iso()
            data = {
                key: clean.__dict__[key]
                for key in clean.__dict__.keys() - _STANDARD_FIELDS - _JSON_FIELDS
            }
            data.update(ts=timestamp, level=clean.levelname, logger=clean.name, msg=clean.msg)
            if clean.exc_text:
                data["exception"] = clean.exc_text
            if clean.stack_info:
                data["stack"] = clean.stack_info
            line = json.dumps(data) + "\n"
            path = self._directory / f"nimbo-{timestamp[:10]}.jsonl"
            with os.fdopen(os.open(path, _APPEND, 0o600), "a", encoding="utf-8") as handle:
                if _private_log(handle.fileno()):
                    handle.write(line)
        if self._stderr is not None:
            with suppress(Exception):
                self._stderr.write(self._human.format(clean) + "\n")
                self._stderr.flush()


def _daily_logs(directory: Path) -> list[tuple[date, Path]]:
    files = []
    with suppress(OSError):
        for path in directory.glob("nimbo-*.jsonl"):
            match = _LOG_NAME.fullmatch(path.name)
            if match is not None:
                with suppress(ValueError):
                    files.append((date.fromisoformat(match[1]), path))
    return files


def setup_logging(paths: Paths, *, verbose: bool = False, stderr: bool = True) -> None:
    """Install the daily JSONL handler and optional stderr output, ignoring sink failures."""
    with suppress(OSError):
        paths.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cutoff = date.fromisoformat(utc_now_iso()[:10]) - timedelta(days=30)
    for day, path in _daily_logs(paths.log_dir):
        if day < cutoff:
            with suppress(OSError):
                path.unlink()
        else:
            with suppress(OSError):
                fd = os.open(path, _INSPECT)
                try:
                    _private_log(fd)
                finally:
                    os.close(fd)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, _LogHandler):
            root.removeHandler(handler)
            handler.close()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    root.addHandler(_LogHandler(paths.log_dir, level, sys.stderr if stderr else None))


def read_log_tail(paths: Paths, lines: int = 40) -> list[dict[str, Any]]:
    """Return up to lines recent JSON objects in chronological file and line order."""
    if lines <= 0:
        return []

    newest_first: list[dict[str, Any]] = []
    for _, path in sorted(_daily_logs(paths.log_dir), reverse=True):
        entries: deque[dict[str, Any]] = deque(maxlen=lines - len(newest_first))
        with suppress(OSError):
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            with path.open("rb") as handle:
                for line in handle:
                    with suppress(ValueError, RecursionError):
                        value = json.loads(line)
                        if isinstance(value, dict):
                            entries.append(value)
        newest_first.extend(reversed(entries))
        if len(newest_first) == lines:
            break
    newest_first.reverse()
    return newest_first
