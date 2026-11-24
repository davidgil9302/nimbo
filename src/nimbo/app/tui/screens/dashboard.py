"""Live overview and keyboard or mouse navigation."""

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static
from textual.worker import Worker

from nimbo.core.errors import LockedError
from nimbo.core.lock import is_locked
from nimbo.core.model import RunResult

from ..data import Snapshot
from ..widgets.folders import FolderList
from ..widgets.header import Header, Rule
from ..widgets.recent import RecentBackups
from ..widgets.stats import Stats
from ..widgets.transfers import Transfers
from . import FlatButton, Panel, error_text, key_label
from .confirm import ConfirmScreen
from .diagnostics import DiagnosticsScreen
from .history import HistoryScreen
from .pending import PendingScreen
from .settings import SettingsScreen
from .welcome import WelcomeScreen

_ACTIONS = (
    ("r", "backup"),
    ("x", "stop"),
    ("p", "pending"),
    ("h", "history"),
    ("c", "settings"),
    ("d", "diagnostics"),
    ("q", "quit"),
)


class DashboardScreen(Panel[None]):
    """Draw the current snapshot and dispatch the available backup actions."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(key, action, show=False) for key, action in _ACTIONS
    ]

    def __init__(self) -> None:
        super().__init__()
        self._command: Worker[None] | None = None
        self._input: Snapshot | None = None
        self._minute: str | None = None

    def compose(self) -> ComposeResult:
        """Arrange the service header, live content, folders and action bar."""
        t, motion = self.t, self.nimbo.motion
        self._header = Header(motion, t)
        self._stats = Stats(motion, t)
        self._transfers = Transfers(motion)
        self._folders = FolderList(motion)
        self._recent = RecentBackups()
        self._notice = Static("", id="dashboard-notice", markup=False)
        self._notice.display = False
        self._review = FlatButton(t("tui.action.review"), id="review-deletions", classes="flat")
        self._review.display = False
        self._buttons = {
            action: FlatButton(
                key_label(key, t(f"tui.action.{action}")), id=f"nav-{action}", classes="key"
            )
            for key, action in _ACTIONS
        }
        self._buttons["stop"].display = False
        with VerticalScroll(id="dashboard-body"), Vertical(id="dashboard-content"):
            yield self._header
            yield self._stats
            yield self._transfers
            yield self._notice
            yield self._review
            yield Rule(classes="dashboard-rule")
            yield self._folders
            yield self._recent
        yield Vertical(
            Horizontal(*self._buttons.values(), id="dashboard-actions"), id="dashboard-footer"
        )

    async def set_snapshot(self, state: Snapshot) -> None:
        """Reconcile the parts of the view that changed since the last read."""
        t = self.t
        now = self.nimbo.now()
        minute = now.isoformat(timespec="minutes")
        if state == self._input and minute == self._minute:
            return
        self._minute = minute
        previous, self._input = self._input, state
        self._header.set_snapshot(state, t)
        self._stats.set_snapshot(state, now, t)
        self._recent.set_runs(state.recent_runs, now, t)
        self._stats.display = not state.running
        self._buttons["backup"].display = not state.running
        self._buttons["stop"].display = state.running
        for key, action in _ACTIONS:
            button = self._buttons[action]
            label_key = (
                "review_short" if action == "backup" and state.needs_confirmation else action
            )
            button.set_label(key_label(key, t(f"tui.action.{label_key}")))
        self._review.set_label(t("tui.action.review"))
        self._review.display = state.needs_confirmation
        notices = []
        if state.needs_confirmation:
            notices.append(t("tui.confirm.notice"))
        elif (
            not state.running
            and state.last_run
            and state.last_run.result
            in {
                RunResult.PARTIAL,
                RunResult.FAILED,
                RunResult.SKIPPED,
                RunResult.PAUSED,
            }
        ):
            reason = f"reason.{state.last_run.reason}"
            notices.append(t(reason) if t.has(reason) else t(f"result.{state.last_run.result}"))
        notices.extend(t(key) for key in state.errors)
        self._notice.update("\n".join(notices))
        self._notice.display = bool(notices)
        if (
            previous is None
            or previous.folders != state.folders
            or previous.language != state.language
        ):
            await self._folders.set_folders(state.folders, t)
        if (
            previous is None
            or previous.progress != state.progress
            or previous.language != state.language
        ):
            await self._transfers.set_progress(state.progress, t)

    @on(events.Resize)
    def show_recent_when_roomy(self, event: events.Resize) -> None:
        """Use the extra vertical space only on taller terminals."""
        self.set_class(event.size.height >= 36, "roomy")

    @on(events.ScreenResume)
    @on(events.ScreenSuspend)
    def sync_folder_motion(self) -> None:
        """Keep the folder pulse confined to the dashboard."""
        if self.is_mounted:
            self._folders.sync_motion()

    def _start_command(self, *, stop: bool = False) -> None:
        if self._command is not None and not self._command.is_finished:
            return
        t, app = self.t, self.nimbo

        def perform(_: object) -> None:
            if stop:
                app.stop_backup()
            else:
                if is_locked(app.paths.lock_file):
                    raise LockedError()
                app.start_backup(app.paths)

        def finished(_: None) -> None:
            self.notify(
                t("tui.dashboard.stop_requested" if stop else "tui.dashboard.start_requested")
            )
            app.refresh_snapshot()

        def failed(exc: Exception) -> None:
            self.notify(
                error_text(t, exc, "tui.error.stop" if stop else "tui.error.start"),
                severity="error",
            )

        self._command = self.start_job(perform, finished, failed)

    def action_backup(self) -> None:
        """Request a backup, or show the exact pending deletions before approval."""
        state = self._input
        if state is None or state.running:
            return
        if not state.configured:
            self.app.push_screen(WelcomeScreen(state.errors))
        elif state.needs_confirmation and state.last_run is not None:
            self.app.push_screen(ConfirmScreen(state.last_run))
        else:
            self._start_command()

    def action_stop(self) -> None:
        """Request that systemd stop the active backup."""
        if self._input is not None and self._input.running:
            self._start_command(stop=True)

    def action_pending(self) -> None:
        """Open the current plan preview."""
        self.app.push_screen(PendingScreen())

    def action_history(self) -> None:
        """Open recent backup history."""
        self.app.push_screen(HistoryScreen())

    def action_settings(self) -> None:
        """Open the backup configuration editor."""
        self.app.push_screen(SettingsScreen())

    def action_diagnostics(self) -> None:
        """Open checks or instructions for terminal diagnostics."""
        self.app.push_screen(DiagnosticsScreen())

    def action_quit(self) -> None:
        """Exit the dashboard with the normal CLI success code."""
        self.app.exit(0)

    @on(Button.Pressed)
    def on_action_button(self, event: Button.Pressed) -> None:
        """Dispatch a footer click through the same actions as the shortcuts."""
        button = event.button.id
        if button == "review-deletions":
            self.action_backup()
        elif button is not None and button.startswith("nav-"):
            action = {
                "backup": self.action_backup,
                "stop": self.action_stop,
                "pending": self.action_pending,
                "history": self.action_history,
                "settings": self.action_settings,
                "diagnostics": self.action_diagnostics,
                "quit": self.action_quit,
            }[button.removeprefix("nav-")]
            action()
