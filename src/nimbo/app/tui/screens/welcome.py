"""First-run guidance for users who have not set up a backup."""

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Static

from . import FlatButton, Panel, key_label
from .diagnostics import DiagnosticsScreen


class WelcomeScreen(Panel[None]):
    """Explain initial setup and provide access to diagnostics and quit."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("d", "diagnostics", show=False),
        Binding("q", "quit", show=False),
        Binding("escape", "quit", show=False),
    ]

    def __init__(self, errors: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._errors = errors

    def compose(self) -> ComposeResult:
        """Show the setup command in a terminal-native layout."""
        t = self.t
        with VerticalScroll(classes="panel-body welcome-body"):
            yield self.heading("tui.service")
            yield Static(t("tui.welcome.title"), classes="welcome-title", markup=False)
            yield Static(t("tui.welcome.description"), classes="screen-intro muted", markup=False)
            yield Static(t("tui.welcome.command"), classes="command-line", markup=False)
            for error in self._errors:
                yield Static(t(error), classes="notice", markup=False)
        yield Horizontal(
            FlatButton(
                key_label("d", t("tui.action.diagnostics")), id="welcome-diagnostics", classes="key"
            ),
            FlatButton(key_label("q", t("tui.action.quit")), id="welcome-quit", classes="key"),
            classes="panel-footer",
        )

    @on(Button.Pressed, "#welcome-diagnostics")
    def action_diagnostics(self) -> None:
        """Open help for investigating a setup problem."""
        self.app.push_screen(DiagnosticsScreen())

    @on(Button.Pressed, "#welcome-quit")
    def action_quit(self) -> None:
        """Exit from first-run guidance."""
        self.app.exit(0)
