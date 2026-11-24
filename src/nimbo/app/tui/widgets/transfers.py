"""Live transfer rows and a rolling throughput sparkline."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Sparkline, Static

from nimbo.app.i18n import Translator
from nimbo.core.model import ActiveTransfer, Progress, rel_name

from ..theme import ACCENT, Motion
from . import figure, line, size_pair
from .countup import CountUp
from .smoothbar import SmoothBar


class TransferRow(Horizontal):
    """Show an active file, its progress bar and its transferred bytes."""

    def __init__(self, motion: Motion, transfer: ActiveTransfer, t: Translator) -> None:
        super().__init__(classes="transfer-row")
        self._t = t
        self._file_label = Static(
            line(rel_name(transfer.rel)), classes="transfer-name", markup=False
        )
        self._file_label.tooltip = line(transfer.rel)
        self._bar = SmoothBar(motion, classes="mini-bar")
        self._percent = CountUp(motion, formatter=self._format, classes="transfer-percent")
        self._amount = Static("", classes="transfer-size", markup=False)

    def compose(self) -> ComposeResult:
        """Place the filename before the progress meter and figures."""
        yield self._file_label
        yield self._bar
        yield Horizontal(self._percent, self._amount, classes="transfer-figures")

    def set_transfer(self, transfer: ActiveTransfer, t: Translator) -> None:
        """Update the meter and figure in place for the same transfer."""
        self._t = t
        fraction = min(transfer.done, transfer.total) / transfer.total if transfer.total else 0.0
        self._bar.set_coverage(fraction)
        self._percent.value = fraction * 100
        self._percent.refresh()
        sent, total = size_pair(transfer.done, transfer.total, t)
        self._amount.update(t("tui.dashboard.file_size", sent=sent, total=total))

    def _format(self, value: float) -> Text:
        t = self._t
        number = t("tui.percent", value=min(int(value + 1e-6), 100))
        return figure(number, number)


class Transfers(Vertical):
    """Give active work visual priority and plot the last 60 seconds of speed."""

    def __init__(self, motion: Motion) -> None:
        super().__init__(id="transfers")
        self._motion = motion
        self._rows: dict[str, TransferRow] = {}
        self._files = Vertical(classes="transfer-files")
        self._speed_label = Static("", classes="speed-label muted", markup=False)
        self._speed = Sparkline(min_color=ACCENT, max_color=ACCENT, classes="speed-plot")
        self._chart = Horizontal(self._speed_label, self._speed, classes="speed-row")
        self.display = False

    def compose(self) -> ComposeResult:
        """Reserve one line for throughput below the active files."""
        yield self._files
        yield self._chart

    async def set_progress(self, progress: Progress | None, t: Translator) -> None:
        """Reconcile active file rows and replace the latest speed samples."""
        active = {item.rel: item for item in progress.active} if progress else {}
        history = progress.speed_history[-60:] if progress and progress.phase == "transfer" else []
        self.display = bool(active or history)

        for rel in self._rows.keys() - active.keys():
            await self._rows.pop(rel).remove()
        new = [rel for rel in active if rel not in self._rows]
        if new:
            rows = [TransferRow(self._motion, active[rel], t) for rel in sorted(new)]
            self._rows.update(zip(sorted(new), rows, strict=True))
            await self._files.mount(*rows)
        for rel, item in active.items():
            self._rows[rel].set_transfer(item, t)
        self._chart.display = bool(history)
        if history:
            self._speed_label.update(t("tui.dashboard.speed_history"))
            self._speed.data = [0.0] * (60 - len(history)) + history
