"""Thin, animated coverage and transfer bars."""

from rich.text import Text

from ..theme import ACCENT, MUTED, Motion, TweenFigure


def bar_text(width: int, value: float, *, color: str = ACCENT) -> Text:
    """Draw a thin line with a colored fill and a half-cell transition."""
    extent = max(0, width) * min(max(value, 0.0), 1.0)
    filled, half = divmod(round(extent * 2), 2)
    fill = "━" * filled + ("╾" if half else "")
    bar = Text(fill, style=color, no_wrap=True)
    bar.append("─" * (width - len(fill)), f"default {MUTED}")
    return bar


class SmoothBar(TweenFigure):
    """Draw a width-aware bar with a 300 ms ease-out transition."""

    def __init__(
        self,
        motion: Motion,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(motion, duration=0.3, id=id, classes=classes)
        self.paint = ACCENT

    def validate_value(self, value: float) -> float:
        """Clamp the bar's target to 0 to 1."""
        return min(max(value, 0.0), 1.0)

    def set_coverage(self, value: float | None, *, color: str = ACCENT) -> None:
        """Update coverage and accent, using the empty track for unknown values."""
        self.paint = color
        self.value = value if value is not None else 0.0
        self.refresh()

    def render(self) -> Text:
        """Render the interpolated fill against the available content width."""
        return bar_text(self.content_size.width, self.display_value, color=self.paint)
