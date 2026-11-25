"""A formatter-driven figure that interpolates numeric changes."""

from collections.abc import Callable

from rich.text import Text

from ..theme import Motion, TweenFigure


class CountUp(TweenFigure):
    """Display a numeric value in a localized figure with a 400 ms ease-out."""

    def __init__(
        self,
        motion: Motion,
        *,
        formatter: Callable[[float], str | Text],
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(motion, duration=0.4, id=id, classes=classes)
        self._formatter = formatter

    def render(self) -> Text:
        """Apply the formatter to the current value and keep the figure on one line."""
        value = self._formatter(self.display_value)
        text = Text(value) if isinstance(value, str) else value
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text
