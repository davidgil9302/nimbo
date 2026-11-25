"""The service identity and the primary dashboard progress line."""

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from nimbo.app.fmt import human_duration
from nimbo.app.i18n import Translator
from nimbo.core.model import Progress

from ..data import Snapshot
from ..theme import Motion
from . import figure, line, size_text
from .countup import CountUp
from .smoothbar import SmoothBar


def _progress_value(progress: Progress) -> float | None:
    if progress.phase == "transfer" and progress.bytes_total > 0:
        return min(progress.bytes_done, progress.bytes_total) / progress.bytes_total
    if progress.ops_total > 0:
        return min(progress.ops_done, progress.ops_total) / progress.ops_total
    return None


class Header(Vertical):
    """Show the service, target and either live progress or recorded coverage."""

    def __init__(self, motion: Motion, t: Translator) -> None:
        super().__init__(id="dashboard-header")
        self._t = t
        self._state: Snapshot | None = None
        self._title = Static(t("tui.service"), id="service-name", markup=False)
        self._target = Static("", id="target-label", markup=False)
        self._bar = SmoothBar(motion, id="primary-bar")
        self._caption = CountUp(motion, formatter=self._format, id="primary-caption")
        self._fraction: float | None = None

    def compose(self) -> ComposeResult:
        """Lay out identity, bar and caption in reading order."""
        yield Horizontal(self._title, self._target, id="identity")
        yield self._bar
        yield self._caption

    def set_snapshot(self, state: Snapshot, t: Translator) -> None:
        """Update the primary widgets with the latest dashboard state."""
        self._state, self._t = state, t
        self._title.update(line(state.service) or t("tui.service"))
        self._target.update(line(state.target_label))
        self._fraction = (
            _progress_value(state.progress)
            if state.progress
            else None
            if state.running
            else state.coverage
        )
        self._bar.set_coverage(self._fraction)
        self._caption.value = (self._fraction or 0) * 100
        self._caption.refresh()

    def _format(self, value: float) -> Text:
        t, state = self._t, self._state
        number = t("tui.percent", value=min(int(value + 1e-6), 100))
        if state is None:
            return Text(t("tui.loading"))
        if not state.running:
            if self._fraction is None:
                return Text(t("tui.dashboard.no_coverage"))
            return figure(t("tui.dashboard.coverage", percent=number), number)
        progress = state.progress
        if progress is None:
            return Text(t("tui.dashboard.starting"))
        phase = f"phase.{progress.phase}"
        if progress.phase != "transfer":
            label = t(phase) if t.has(phase) else t("tui.dashboard.working")
            if self._fraction is None:
                return Text(label)
            return figure(t("tui.dashboard.with_figure", label=label, percent=number), number)
        label = (
            t("tui.dashboard.backing_up", folder=line(progress.folder))
            if progress.folder
            else t("phase.transfer")
        )
        if self._fraction is None:
            return Text(label)
        text = figure(t("tui.dashboard.with_figure", label=label, percent=number), number)
        if progress.speed_bps > 0:
            speed = t("tui.dashboard.speed", size=size_text(int(progress.speed_bps), t))
            text.append(t("tui.separator"))
            text.append(figure(speed, speed))
        if progress.eta_s is not None:
            text.append(t("tui.separator"))
            text.append(
                t("tui.dashboard.remaining", duration=human_duration(progress.eta_s, t.language))
            )
        return text


class Rule(Static):
    """Draw a single dim line across the current content width."""

    def render(self) -> Text:
        """Repeat a light horizontal rule to the available width."""
        return Text("─" * self.content_size.width, style="dim")


def clock_label(moment: datetime | None, t: Translator) -> str:
    """Format an absolute timestamp for detailed history."""
    return moment.strftime("%Y-%m-%d %H:%M") if moment else t("common.unknown")
