"""Colored folder states and a braille spinner for active work."""

from math import cos, tau

from rich.text import Text
from textual.color import Color
from textual.reactive import reactive
from textual.widget import Widget

from ..data import FolderState
from ..theme import ACCENT, ATTENTION, ERROR, MUTED, OK, Motion

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ACCENT_RGB = Color.parse(ACCENT)
_GRAY = Color(128, 128, 128)
_STATES: dict[FolderState, tuple[str, str]] = {
    "up_to_date": ("●", OK),
    "changes": ("◐", ATTENTION),
    "running": ("◌", ACCENT),
    "waiting": ("·", MUTED),
    "no_disk": ("○", MUTED),
    "error": ("●", ERROR),
    "paused": ("◦", MUTED),
    "unknown": ("○", MUTED),
}


class PulseDot(Widget):
    """Represent a folder state in one cell and pulse only while visibly active."""

    state = reactive[FolderState]("unknown")

    def __init__(self, motion: Motion, state: FolderState, *, classes: str | None = None) -> None:
        super().__init__(classes=classes)
        self._motion = motion
        self._phase = 0.0
        self.state = state

    def on_mount(self) -> None:
        """Start the pulse if the first state is already running."""
        self.sync_motion()

    def on_unmount(self) -> None:
        """Release the pulse callback when the dot is removed."""
        self._motion.forget(self._frame)

    def watch_state(self) -> None:
        """Start or stop animation when the state changes."""
        self.sync_motion()

    def sync_motion(self) -> None:
        """Synchronize the pulse after a screen becomes visible or hidden."""
        if self.is_mounted and self.screen.is_current and self.state == "running":
            self._motion.track(self._frame)
        else:
            self._phase = 0
            self._motion.forget(self._frame)
        self.refresh()

    def _frame(self, now: float) -> bool:
        if not self.screen.is_current:
            return False
        self._phase = now % 1.0
        self.refresh()
        return True

    def render(self) -> Text:
        """Render the state glyph using its state color or the terminal's dim style."""
        glyph, style = _STATES[self.state]
        if self.state == "running" and not self._motion.reduced:
            glyph = _SPINNER[int(self._phase * len(_SPINNER))]
            strength = (1 + cos(tau * self._phase)) / 2
            if strength < 0.18:
                style = MUTED
            else:
                style = _GRAY.blend(_ACCENT_RGB, strength).hex
                if strength < 0.5:
                    style += " dim"
        return Text(glyph, style=style)
