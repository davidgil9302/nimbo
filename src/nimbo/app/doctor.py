"""Collect independent readiness checks for the local mirror and its service."""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from rich.console import Console
from rich.text import Text

from nimbo import __version__
from nimbo.core.config import Config, load_config
from nimbo.core.errors import ConfigError, NimboError
from nimbo.core.index import META_ROOT_UUID, Index
from nimbo.core.model import RunResult
from nimbo.core.paths import Paths
from nimbo.core.safety import check_source
from nimbo.core.secrets import REFRESH_TOKEN_KEY

from . import context, service
from .context import safe_label
from .fmt import human_size
from .i18n import Translator


class Check(NamedTuple):
    """Report readiness, failure impact, and the message and hint keys."""

    ok: bool
    critical: bool
    message: str
    hint: str


def _check(
    item: str, ok: bool, *, critical: bool = True, **values: str
) -> tuple[Check, dict[str, str]]:
    return Check(
        ok, critical, f"doctor.{item}.{'ok' if ok else 'bad'}", f"doctor.{item}.hint"
    ), values


def _cloud_checks(cfg: Config | None) -> Iterator[tuple[Check, dict[str, str]]]:
    hooks = context.test_hooks(os.environ)
    store = context.secret_store(hooks)
    try:
        store.get(REFRESH_TOKEN_KEY)
        yield _check("keyring", True)
    except NimboError:
        yield _check("keyring", False)
        return

    if cfg is None:
        return
    with context.new_http_client(loopback=hooks.api_base is not None) as http:
        tokens = context.token_provider(cfg.oauth, store, http, hooks)
        drive = context.make_drive(http, tokens, cfg.transfer, hooks)
        try:
            authorized = bool(tokens.token())
        except NimboError:
            authorized = False
        yield _check("token", authorized)
        if not authorized:
            return
        try:
            drive.about()
        except NimboError:
            yield _check("api", False)
            return
        yield _check("api", True)
        try:
            item = drive.get(cfg.target.folder_id)
        except NimboError:
            item = None
        if item is not None and item.is_folder and not item.trashed:
            yield _check("target", True, name=safe_label(item.name))
        else:
            yield _check("target", False)


def _existing_parent(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return path


def _checks(paths: Paths, *, language: str) -> Iterator[tuple[Check, dict[str, str]]]:
    try:
        cfg = load_config(paths.config_file)
    except ConfigError:
        cfg = None
    yield _check("config", cfg is not None)
    yield from _cloud_checks(cfg)

    root_uuid = None
    last = None
    last_readable = True
    try:
        context.validate_state_paths(paths)
        with closing(Index.open_readonly(paths.db_file)) as index:
            integrity = index.integrity_ok()
            if integrity:
                root_uuid = index.get_meta(META_ROOT_UUID)
                try:
                    last = index.last_run()
                except (ValueError, TypeError, RecursionError):
                    last_readable = False
    except (NimboError, OSError, ValueError, sqlite3.Error):
        integrity = False
    if cfg is not None:
        problems = {problem.code for problem in check_source(cfg, root_uuid)}
        yield _check("source", problems.isdisjoint({"source_missing", "source_not_mounted"}))
        yield _check("sentinel", problems.isdisjoint({"sentinel_missing", "sentinel_mismatch"}))
        yield _check("folders", not any(code.startswith("folder_") for code in problems))

    units = service.service_state()
    yield _check("units", units.installed and units.timer_enabled, critical=False)
    yield _check(
        "next",
        units.next_run is not None,
        critical=False,
        time=units.next_run.astimezone().isoformat(timespec="minutes") if units.next_run else "",
    )
    yield _check("db", integrity)
    if last is None:
        yield _check("no_run", last_readable, critical=False)
    else:
        yield _check(
            "last",
            last.result is RunResult.OK,
            critical=False,
            result=Translator(language)(f"result.{last.result}"),
        )
    try:
        free = shutil.disk_usage(_existing_parent(paths.state_dir.absolute())).free
        yield _check("space", free > 0, space=human_size(free, language))
    except OSError:
        yield _check("space", False)
    yield _check("version", True, critical=False, version=__version__)


def run_doctor(paths: Paths, *, t: Translator, console: Console) -> int:
    """Print check outcomes and return 1 if a critical readiness check failed."""
    healthy = True
    for check, values in _checks(paths, language=t.language):
        mark = Text("✓ " if check.ok else "✗ ", style="green" if check.ok else "yellow")
        mark.append(t(check.message, **values))
        console.print(mark)
        if not check.ok:
            console.print(Text("  " + t(check.hint), style="dim"))
            healthy &= not check.critical
    return 0 if healthy else 1
