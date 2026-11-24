"""Widgets and formatting shared by the terminal screens."""

from datetime import datetime, timedelta

from rich.text import Text

from nimbo.app.context import safe_label
from nimbo.app.fmt import human_size, relative_time
from nimbo.app.i18n import Translator
from nimbo.core.model import RunResult

from ..theme import ACCENT, ATTENTION, ERROR, MUTED, OK

_RUN_MARKERS = {
    RunResult.OK: ("●", OK),
    RunResult.PARTIAL: ("◐", ATTENTION),
    RunResult.NEEDS_CONFIRMATION: ("◐", ATTENTION),
    RunResult.PAUSED: ("◐", ATTENTION),
    RunResult.FAILED: ("●", ERROR),
}


def line(value: str) -> str:
    """Escape nonprintable characters in paths and labels as the CLI does."""
    return safe_label(value)


def figure(value: str, *highlights: str, color: str = ACCENT) -> Text:
    """Style only the named numeric fragments in a localized line."""
    rendered = Text(value)
    for highlight in highlights:
        start = value.find(highlight)
        if highlight and start >= 0:
            rendered.stylize(f"bold {color}", start, start + len(highlight))
    return rendered


def run_status(result: RunResult, t: Translator) -> Text:
    """Pair a localized run result with its dashboard state marker."""
    glyph, style = _RUN_MARKERS.get(result, ("○", MUTED))
    return Text.assemble((glyph, style), " ", t(f"result.{result}"))


def size_text(size: int | None, t: Translator) -> str:
    """Use the shared size formatter, omitting a redundant decimal zero."""
    value = human_size(size, t.language)
    return value.replace(t("units.decimal_separator") + "0 ", " ")


def size_pair(done: int, total: int, t: Translator) -> tuple[str, str]:
    """Use a common unit once when both formatted sizes have the same unit."""
    left, right = size_text(done, t), size_text(total, t)
    left_value, _, left_unit = left.rpartition(" ")
    return (left_value if left_unit == right.rpartition(" ")[2] else left), right


def date_text(moment: datetime | None, now: datetime, t: Translator) -> str:
    """Format a backup date, including the dashboard's common tomorrow case."""
    if moment is not None:
        local = moment.astimezone(now.tzinfo) if moment.tzinfo and now.tzinfo else moment
        if local.date() - now.date() == timedelta(days=1):
            return t("tui.tomorrow", time=local.strftime("%H:%M"))
    return relative_time(moment, now, t)
