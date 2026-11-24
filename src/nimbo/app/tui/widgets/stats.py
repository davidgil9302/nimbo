"""The dashboard's three aligned backup statistics."""

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from nimbo.app.i18n import Translator

from ..data import Snapshot, parse_time
from ..theme import Motion
from . import date_text, figure, size_pair, size_text
from .countup import CountUp
from .smoothbar import SmoothBar


class Stats(Horizontal):
    """Align the most recent run, next scheduled run and used Drive space."""

    def __init__(self, motion: Motion, t: Translator) -> None:
        super().__init__(id="dashboard-stats")
        self._t = t
        self._state: Snapshot | None = None
        self._labels = [Static("", classes="stat-label", markup=False) for _ in range(3)]
        self._last = Static("", classes="stat-value", markup=False)
        self._next = Static("", classes="stat-value", markup=False)
        self._quota = CountUp(motion, formatter=self._format_quota, id="quota-value")
        self._bar = SmoothBar(motion, classes="mini-bar", id="quota-bar")

    def compose(self) -> ComposeResult:
        """Place each value under its label and the quota meter under its value."""
        yield Vertical(self._labels[0], self._last, classes="stat")
        yield Vertical(self._labels[1], self._next, classes="stat")
        yield Vertical(self._labels[2], self._quota, self._bar, classes="stat")

    def set_snapshot(self, state: Snapshot, now: datetime, t: Translator) -> None:
        """Refresh values and interpret scheduled timestamps in the local calendar."""
        self._state, self._t = state, t
        for label, key in zip(self._labels, ("last", "next", "space"), strict=True):
            label.update(t(f"tui.dashboard.{key}"))
        moment = (
            parse_time(state.last_run.finished_at or state.last_run.started_at)
            if state.last_run
            else None
        )
        self._last.update(date_text(moment, now, t))
        self._next.update(date_text(state.next_run, now, t))
        self._quota.value = float(state.quota_used or 0)
        self._quota.refresh()
        self._bar.display = state.quota_used is not None and bool(state.quota_total)
        if state.quota_used is not None and state.quota_total:
            self._bar.set_coverage(state.quota_used / state.quota_total)

    def _format_quota(self, value: float) -> Text:
        t, state = self._t, self._state
        if state is None or state.quota_used is None:
            return Text(t("common.unknown"))
        used = size_text(int(value), t)
        if state.quota_total is None:
            return figure(t("tui.dashboard.quota_used", size=used), used)
        used, total = size_pair(int(value), state.quota_total, t)
        return figure(t("tui.dashboard.quota", used=used, total=total), used)
