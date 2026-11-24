"""Terminal-native colors and the shared, demand-driven motion clock."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from time import monotonic

from textual.app import App
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget

ACCENT = "#4C8BF5"
OK = "#5FBF8A"
ATTENTION = "#E8B04B"
ERROR = "#EF6F6C"
MUTED = "dim"


def reduce_motion(requested: bool, env: Mapping[str, str] | None = None) -> bool:
    """Disable motion on request, over SSH or on terminals without truecolor."""
    env = os.environ if env is None else env
    return (
        requested
        or env.get("NIMBO_REDUCED_MOTION") == "1"
        or "SSH_CONNECTION" in env
        or env.get("COLORTERM", "").lower() not in {"truecolor", "24bit"}
    )


class Motion:
    """Run one 10 Hz Textual timer only while a widget is actively animating."""

    def __init__(
        self, app: App[int] | App[None], *, reduced: bool, clock: Callable[[], float] = monotonic
    ) -> None:
        self.reduced = reduced
        self.clock = clock
        self._app = app
        self._timer: Timer | None = None
        self._frames: set[Callable[[float], bool]] = set()

    @property
    def active(self) -> bool:
        """Return whether any animation is scheduled."""
        return self._timer is not None

    def track(self, frame: Callable[[float], bool]) -> None:
        """Call a frame step until it returns false or is explicitly removed."""
        if not self.reduced:
            self._frames.add(frame)
            if self._timer is None:
                self._timer = self._app.set_interval(0.1, self.tick, name="dashboard motion")

    def forget(self, frame: Callable[[float], bool]) -> None:
        """Remove a frame callback and stop the timer when no work remains."""
        self._frames.discard(frame)
        if not self._frames and self._timer is not None:
            self._timer.stop()
            self._timer = None

    def tick(self) -> None:
        """Advance every scheduled animation using the same clock reading."""
        now = self.clock()
        for frame in tuple(self._frames):
            if not frame(now):
                self.forget(frame)


class TweenFigure(Widget):
    """Interpolate a reactive numeric value when its screen is visible."""

    value = reactive(0.0, repaint=False)
    display_value = reactive(0.0)

    def __init__(
        self, motion: Motion, *, duration: float, id: str | None = None, classes: str | None = None
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._motion = motion
        self._duration = duration
        self._started = 0.0
        self._previous = 0.0

    def validate_value(self, value: float) -> float:
        """Normalize the animation target. Subclasses may restrict its range."""
        return value

    def watch_value(self, value: float) -> None:
        """Start an ease-out tween from the value currently shown."""
        if self._motion.reduced or not self.is_mounted or not self.screen.is_current:
            self.display_value = value
            self._motion.forget(self._frame)
        elif value != self.display_value:
            self._previous = self.display_value
            self._started = self._motion.clock()
            self._motion.track(self._frame)
        else:
            self._motion.forget(self._frame)

    def _frame(self, now: float) -> bool:
        elapsed = (now - self._started) / self._duration
        if elapsed >= 1 or not self.screen.is_current:
            self.display_value = self.value
            return False
        self.display_value = self._previous + (self.value - self._previous) * (
            1 - (1 - elapsed) ** 3
        )
        return True

    def on_unmount(self) -> None:
        """Unregister callbacks when this widget leaves the DOM."""
        self._motion.forget(self._frame)
