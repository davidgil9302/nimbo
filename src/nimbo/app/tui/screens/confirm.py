"""Review the exact deletion proposal before approving a new backup."""

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

from nimbo.core.errors import LockedError, NimboError
from nimbo.core.fsutil import read_json
from nimbo.core.lock import is_locked
from nimbo.core.model import RunResult, RunSummary
from nimbo.core.safety import write_approval

from ..data import load_runs
from ..widgets import line
from . import FlatButton, Panel, error_text

_TTL = timedelta(hours=24)


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
    """List a paused run's proposed deletions and ask for explicit approval."""

    def __init__(self, run: RunSummary) -> None:
        super().__init__()
        self._run = run
        self._worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        """Show the preview in a scrollable body with a persistent confirm action."""
        t = self.t
        enabled = bool(self._run.fingerprint and self._run.trash_preview)
        self._confirm = FlatButton(
            t("tui.action.confirm"),
            id="confirm-start",
            classes="flat",
            disabled=not enabled,
        )
        self._status = Static(
            "" if enabled else t("tui.confirm.stale"),
            id="confirm-status",
            classes="notice",
            markup=False,
        )
        with VerticalScroll(classes="panel-body"):
            yield self.heading("tui.confirm.title", t("tui.confirm.description"))
            if len(self._run.trash_preview) >= 500:
                yield Static(t("tui.confirm.preview_limit"), classes="notice", markup=False)
            for item in self._run.trash_preview:
                path, delimiter, count = item.rpartition("\t")
                if delimiter and count.isascii() and count.isdecimal() and len(count) <= 20:
                    shown = t(
                        "tui.confirm.path",
                        path=line(path),
                        files=t.plural("tui.confirm.files", int(count)),
                    )
                else:
                    shown = line(item)
                yield Static(shown, classes="deletion-row", markup=False)
            yield self._status
        yield self.footer(self._confirm)

    def action_back(self) -> None:
        """Keep the start outcome visible while the worker is active."""
        if self._worker is None or self._worker.is_finished:
            super().action_back()

    @on(Button.Pressed, "#confirm-start")
    def approve(self) -> None:
        """Recheck the latest run, write its fingerprint and queue a backup."""
        fingerprint = self._run.fingerprint
        if self._confirm.disabled or not fingerprint:
            return
        t, app = self.t, self.nimbo
        self._confirm.disabled = True
        self._status.update(t("tui.confirm.working"))

        def work(_: Event) -> None:
            if app.service_state().running or is_locked(app.paths.lock_file):
                raise LockedError()
            runs = load_runs(app.paths, limit=1)
            if (
                not runs
                or runs[0] != self._run
                or runs[0].result is not RunResult.NEEDS_CONFIRMATION
            ):
                raise NimboError(code="tui.confirm.stale")
            now = app.now()
            path = app.paths.approval_file
            write_approval(path, fingerprint, now=now, ttl=_TTL)
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
            self._confirm.disabled = stale
            if stale:
                app.refresh_snapshot()

        self._worker = self.start_job(work, finished, failed)
