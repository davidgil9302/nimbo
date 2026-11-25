"""Review proposed deletions and overwrites before approving a backup."""

import fcntl
import os
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Static
from textual.worker import Worker

from nimbo.app.context import validate_state_paths
from nimbo.core.config import Config, load_config
from nimbo.core.errors import LockedError, NimboError
from nimbo.core.fsutil import read_json
from nimbo.core.lock import RunLock, is_locked
from nimbo.core.model import OpKind, RunResult, RunSummary
from nimbo.core.safety import OVERWRITE_CONFIRMATION, write_approval

from ..data import load_runs
from ..widgets import line
from . import FlatButton, Panel, error_text

_TTL = timedelta(hours=24)
_PREVIEW_LIMIT = 500


def _preview_entry(item: str) -> tuple[str, int | None]:
    path, delimiter, count = item.rpartition("\t")
    if delimiter and count.isascii() and count.isdecimal() and len(count) <= 20:
        return path, int(count)
    return item, None


def _revoke(path: Path, fingerprint: str, expires: datetime) -> None:
    # Use the same directory lock as write_approval/consume_approval; a newer
    # approval from another CLI or dashboard must survive a failed start.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if read_json(path) == {"fingerprint": fingerprint, "expires": expires.isoformat()}:
            path.unlink(missing_ok=True)
    finally:
        os.close(fd)


class ConfirmScreen(Panel[None]):
    """List the changes awaiting approval for a paused run."""

    def __init__(self, run: RunSummary) -> None:
        super().__init__()
        self._run = run
        self._overwrites = run.reason == OVERWRITE_CONFIRMATION
        self._config: Config | None = None
        self._worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        """Show the preview in a scrollable body with a persistent confirm action."""
        t = self.t
        self._confirm = FlatButton(
            t("tui.action.confirm"),
            id="confirm-start",
            classes="flat",
            disabled=True,
        )
        self._status = Static(
            t("tui.loading"),
            id="confirm-status",
            classes="notice",
            markup=False,
        )
        self._total = Static("", id="confirm-total", classes="panel-content", markup=False)
        self._rows = Static("", classes="deletion-row", markup=False)
        self._total.display = self._rows.display = False
        with VerticalScroll(classes="panel-body"):
            if self._overwrites:
                yield self.heading("tui.confirm.changes_title", t("reason.overwrite_confirmation"))
            else:
                yield self.heading("tui.confirm.title", t("tui.confirm.description"))
            yield self._status
            yield self._total
            yield self._rows
        yield self.footer(self._confirm)

    def on_mount(self) -> None:
        """Prepare the entire saved or reconstructed proposal before offering approval."""
        if not self._run.fingerprint or not (self._overwrites or self._run.trash_preview):
            self._status.update(self.t("tui.confirm.stale"))
            return
        self.start_job(self._load_review, self._show_review, self._review_failed)

    def _load_review(self, stop: Event) -> tuple[Config, list[tuple[OpKind, str, int | None]]]:
        app = self.nimbo
        cfg = load_config(app.paths.config_file)
        if not self._overwrites and len(self._run.trash_preview) < _PREVIEW_LIMIT:
            entries = [(OpKind.TRASH, *_preview_entry(item)) for item in self._run.trash_preview]
        else:
            factory = app.engine_factory
            if factory is None:
                raise NimboError(code="tui.error.preview")
            if app.service_state().running or is_locked(app.paths.lock_file):
                raise LockedError()
            engine, close = factory()
            try:
                preview = engine.preview(stop=stop)
            finally:
                close()
            if preview.plan.errors or preview.brake.fingerprint != self._run.fingerprint:
                raise NimboError(code="tui.confirm.stale")
            entries = [
                (op.kind, op.rel, op.affected_files)
                for op in sorted(preview.plan.ops, key=lambda op: op.rel)
                if op.kind is OpKind.TRASH or (self._overwrites and op.kind is OpKind.UPDATE)
            ]
        if not entries:
            raise NimboError(code="tui.confirm.stale")
        self._check_current(cfg)
        return cfg, entries

    def _check_current(self, cfg: Config) -> None:
        paths = self.nimbo.paths
        runs = load_runs(paths, limit=1)
        if (
            not runs
            or runs[0] != self._run
            or runs[0].result is not RunResult.NEEDS_CONFIRMATION
            or load_config(paths.config_file) != cfg
        ):
            raise NimboError(code="tui.confirm.stale")

    def _show_review(self, review: tuple[Config, list[tuple[OpKind, str, int | None]]]) -> None:
        cfg, entries = review
        t = self.t
        rows = []
        for kind, path, count in entries:
            label = line(path)
            if kind is OpKind.UPDATE:
                label = t("tui.confirm.overwrite_path", path=label)
            elif count is not None:
                label = t(
                    "tui.confirm.path", path=label, files=t.plural("tui.confirm.files", count)
                )
                if self._overwrites:
                    label = t("tui.confirm.trash_path", path=label)
            rows.append(label)
        total = "tui.confirm.changes" if self._overwrites else "tui.confirm.items"
        self._total.update(t.plural(total, len(entries)))
        self._rows.update("\n\n".join(rows))
        self._status.display = False
        self._total.display = self._rows.display = True
        self._config = cfg
        self._confirm.disabled = False

    def _review_failed(self, exc: Exception) -> None:
        t = self.t
        self._status.update(
            t(exc.code)
            if isinstance(exc, NimboError) and exc.code == "tui.confirm.stale"
            else error_text(t, exc, "tui.error.preview")
        )

    def action_back(self) -> None:
        """Keep the start outcome visible while the worker is active."""
        if self._worker is None or self._worker.is_finished:
            super().action_back()

    @on(Button.Pressed, "#confirm-start")
    def approve(self) -> None:
        """Recheck the run and configuration, write approval and queue a backup."""
        fingerprint, cfg = self._run.fingerprint, self._config
        if self._confirm.disabled or not fingerprint or cfg is None:
            return
        t, app = self.t, self.nimbo
        self._confirm.disabled = True
        self._status.update(t("tui.confirm.working"))
        self._status.display = True

        def work(_: Event) -> None:
            path = app.paths.approval_file
            validate_state_paths(app.paths)
            with RunLock(app.paths.lock_file):
                if app.service_state().running:
                    raise LockedError()
                self._check_current(cfg)
                now = app.now()
                write_approval(path, fingerprint, now=now, ttl=_TTL)
            # The service needs the run lock as soon as it starts.
            try:
                app.start_backup(app.paths)
            except Exception:
                with suppress(OSError):
                    _revoke(path, fingerprint, now + _TTL)
                raise

        def finished(_: None) -> None:
            self.dismiss()
            self.notify(t("tui.dashboard.start_requested"))
            app.refresh_snapshot()

        def failed(exc: Exception) -> None:
            stale = isinstance(exc, NimboError) and exc.code == "tui.confirm.stale"
            self._status.update(
                t("tui.confirm.stale") if stale else error_text(t, exc, "tui.error.approval")
            )
            self._status.scroll_visible(animate=False)
            self._confirm.disabled = stale
            if stale:
                app.refresh_snapshot()

        self._worker = self.start_job(work, finished, failed)
