"""A compact summary of recent completed runs for taller dashboards."""

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from nimbo.app.i18n import Translator
from nimbo.core.model import RunSummary

from ..data import parse_time
from . import date_text, run_status, size_text


class _RecentRow(Horizontal):
    def __init__(self) -> None:
        super().__init__(classes="recent-row")
        self._date = Static("", classes="recent-date muted", markup=False)
        self._status = Static("", classes="recent-result", markup=False)
        self._files = Static("", classes="recent-files muted", markup=False)
        self._amount = Static("", classes="recent-size muted", markup=False)

    def compose(self) -> ComposeResult:
        yield self._date
        yield self._status
        yield self._files
        yield self._amount

    def set_run(self, run: RunSummary, now: datetime, t: Translator) -> None:
        stamp = parse_time(run.finished_at or run.started_at)
        self._date.update(date_text(stamp, now, t))
        self._status.update(run_status(run.result, t))
        self._files.update(t.plural("tui.confirm.files", run.uploaded + run.updated))
        self._amount.update(size_text(run.uploaded_bytes, t))


class RecentBackups(Vertical):
    """Present up to four recent backups in date, result, files and size columns."""

    def __init__(self) -> None:
        super().__init__(id="recent-backups")
        self._title = Static("", classes="recent-title", markup=False)
        self._rows = [_RecentRow() for _ in range(4)]

    def compose(self) -> ComposeResult:
        """Place a quiet section heading and leave unused rows hidden."""
        yield self._title
        yield from self._rows

    def set_runs(self, runs: tuple[RunSummary, ...], now: datetime, t: Translator) -> None:
        """Update the rows using the dashboard's latest read-only history."""
        self.set_class(bool(runs), "has-runs")
        self._title.update(t("tui.dashboard.recent"))
        for index, row in enumerate(self._rows):
            row.display = index < len(runs)
            if row.display:
                row.set_run(runs[index], now, t)
