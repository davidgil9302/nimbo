"""Diagnostic results supplied by the caller, with a CLI fallback."""

from threading import Event

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..theme import ERROR, OK
from ..widgets import line
from . import Panel, error_text


class DiagnosticsScreen(Panel[None]):
    """List the available checks or show the localized diagnostics command."""

    def compose(self) -> ComposeResult:
        """Build a readable, scrollable check list."""
        t = self.t
        with VerticalScroll(classes="panel-body"):
            yield self.heading("tui.action.diagnostics")
            yield Static(
                t("tui.loading" if self.nimbo.diagnostics else "tui.diagnostics.command"),
                id="diagnostics-body",
                classes="panel-content",
                markup=False,
            )
        yield self.footer()

    def on_mount(self) -> None:
        """Run injected diagnostics off the UI thread when available."""
        checks = self.nimbo.diagnostics
        if checks is None:
            return

        def work(_: Event) -> list[tuple[bool, str]]:
            return checks()

        target = self.query_one("#diagnostics-body", Static)
        t = self.t

        def show(results: list[tuple[bool, str]]) -> None:
            text = Text()
            for valid, message in results:
                if text:
                    text.append("\n")
                text.append("✓ " if valid else "✗ ", OK if valid else ERROR)
                text.append(line(message))
            target.update(text if results else t("tui.diagnostics.empty"))

        self.start_job(
            work, show, lambda exc: target.update(error_text(t, exc, "tui.error.diagnostics"))
        )
