"""Recent runs, their details and the explicit undo workflow."""

from threading import Event

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker

from nimbo.core.errors import LockedError
from nimbo.core.lock import is_locked
from nimbo.core.model import RunSummary, UndoSummary

from ..data import load_runs, parse_time
from ..widgets import line, run_status, size_text
from ..widgets.header import clock_label
from . import FlatButton, Panel, error_text


class HistoryScreen(Panel[None]):
    """Show recent runs in a table with keyboard and mouse access to details."""

    def __init__(self) -> None:
        super().__init__()
        self._runs: dict[str, RunSummary] = {}

    def compose(self) -> ComposeResult:
        """Place the table under a concise title."""
        self._status = Static(self.t("tui.loading"), id="history-status", markup=False)
        self._table: DataTable[Text] = DataTable(
            id="history-table",
            cursor_type="row",
            show_row_labels=False,
            zebra_stripes=False,
            cell_padding=1,
        )
        self._table.display = False
        with Vertical(classes="panel-body"):
            yield self.heading("tui.action.history")
            yield self._status
            yield self._table
        yield self.footer()

    def on_mount(self) -> None:
        """Load up to 50 finished runs through the read-only index."""
        paths = self.nimbo.paths
        self.start_job(
            lambda _: load_runs(paths),
            self._show,
            lambda exc: self._status.update(error_text(self.t, exc, "tui.error.index")),
        )

    def _show(self, runs: list[RunSummary]) -> None:
        t = self.t
        if not runs:
            self._status.update(t("tui.history.empty"))
            return
        for column in ("date", "result", "uploaded", "updated", "trashed"):
            self._table.add_column(t(f"tui.history.{column}"))
        for i, run in enumerate(runs):
            self._runs[str(i)] = run
            stamp = parse_time(run.started_at)
            if stamp is not None and stamp.tzinfo:
                stamp = stamp.astimezone(self.nimbo.now().tzinfo)
            self._table.add_row(
                Text(clock_label(stamp, t)),
                run_status(run.result, t),
                Text(str(run.uploaded), justify="right"),
                Text(str(run.updated), justify="right"),
                Text(str(run.trashed), justify="right"),
                key=str(i),
            )
        self._status.display = False
        self._table.display = True
        self._table.focus()

    @on(DataTable.RowSelected, "#history-table")
    def detail(self, event: DataTable.RowSelected) -> None:
        """Open the selected run when the table activates a row."""
        if event.row_key.value is not None:
            self.app.push_screen(RunDetailScreen(self._runs[event.row_key.value]))


class RunDetailScreen(Panel[None]):
    """Show the outcome of one backup and offer restoration if it trashed items."""

    def __init__(self, run: RunSummary) -> None:
        super().__init__()
        self._run = run

    def compose(self) -> ComposeResult:
        """Present the timestamp, summary and any operation failures."""
        t, run = self.t, self._run
        stamp = parse_time(run.started_at)
        if stamp is not None and stamp.tzinfo:
            stamp = stamp.astimezone(self.nimbo.now().tzinfo)
        self._undo = FlatButton(t("tui.action.undo"), id="undo-run", classes="flat")
        self._undo.display = bool(run.trashed and run.run_id is not None)
        if self.nimbo.engine_factory is None:
            self._undo.disabled = True
        self._outcome = Static("", id="undo-outcome", classes="notice", markup=False)
        self._outcome.display = False
        with VerticalScroll(classes="panel-body"):
            yield self.heading("tui.history.detail", clock_label(stamp, t))
            yield Static(t(f"result.{run.result}"), classes="section-heading", markup=False)
            reason = f"reason.{run.reason}"
            if t.has(reason):
                yield Static(t(reason), classes="muted", markup=False)
            yield Static(
                t(
                    "tui.history.totals",
                    uploaded=run.uploaded,
                    updated=run.updated,
                    moved=run.moved,
                    trashed=run.trashed,
                    size=size_text(run.uploaded_bytes, t),
                ),
                classes="panel-content",
                markup=False,
            )
            yield self._outcome
            for path, code in run.failed:
                reason = f"reason.{code}"
                yield Static(
                    t(
                        "tui.history.failed_at",
                        path=line(path),
                        message=t(reason if t.has(reason) else "tui.error.operation"),
                    ),
                    markup=False,
                )
        yield self.footer(self._undo)

    @on(Button.Pressed, "#undo-run")
    def ask_undo(self) -> None:
        """Require explicit confirmation before opening the engine for undo."""
        self.app.push_screen(UndoScreen(self._run), self._show_undo)

    def _show_undo(self, summary: UndoSummary | None) -> None:
        if summary is None:
            return
        t = self.t
        status = [
            t(
                "tui.history.undo_summary",
                untrashed=summary.untrashed,
                restored=summary.restored_files,
                skipped=len(summary.skipped_existing),
                failed=len(summary.failed),
            )
        ]
        for path, code in summary.failed:
            reason = f"reason.{code}"
            status.append(
                t(
                    "tui.history.failed_at",
                    path=line(path),
                    message=t(reason if t.has(reason) else "tui.error.operation"),
                )
            )
        self._outcome.update("\n".join(status))
        self._outcome.display = True
        self._undo.disabled = not summary.failed


class UndoScreen(Panel[UndoSummary]):
    """Confirm a restore and keep the user informed while the engine runs."""

    def __init__(self, run: RunSummary) -> None:
        super().__init__()
        self._run = run
        self._worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        """Show the restore consequences before offering the action."""
        t = self.t
        self._confirm = FlatButton(t("tui.action.undo"), id="undo-confirm", classes="flat")
        self._status = Static("", id="undo-status", classes="notice", markup=False)
        with Vertical(classes="panel-body"):
            yield self.heading(
                "tui.action.undo", t("tui.history.undo_description", count=self._run.trashed)
            )
            yield self._status
        yield self.footer(self._confirm)

    def action_back(self) -> None:
        """Keep a restore visible until its worker reports an outcome."""
        if self._worker is None or self._worker.is_finished:
            super().action_back()

    @on(Button.Pressed, "#undo-confirm")
    def restore(self) -> None:
        """Start the confirmed undo using a fresh engine instance on a worker."""
        app, factory = self.nimbo, self.nimbo.engine_factory
        if self._confirm.disabled or factory is None:
            return
        self._confirm.disabled = True
        self._status.update(self.t("tui.history.undo_working"))

        def work(stop: Event) -> UndoSummary:
            if is_locked(app.paths.lock_file) or app.service_state().running:
                raise LockedError()
            engine, close = factory()
            try:
                return engine.undo(self._run.run_id, stop=stop)
            finally:
                close()

        def finished(summary: UndoSummary) -> None:
            # The screen stack carries the result back to the run detail.
            self.dismiss(summary)
            app.refresh_snapshot()

        def failed(exc: Exception) -> None:
            self._confirm.disabled = False
            self._status.update(error_text(self.t, exc, "tui.error.undo"))

        self._worker = self.start_job(work, finished, failed)
