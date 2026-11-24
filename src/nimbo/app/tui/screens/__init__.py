"""Layout and worker lifecycle shared by the dashboard screens."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, ClassVar, TypeVar, cast

from rich.text import Text
from textual import events, on
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Static
from textual.worker import Worker, get_current_worker

from nimbo.app.i18n import Translator
from nimbo.core.errors import NimboError

if TYPE_CHECKING:
    from ..app import NimboApp

_T = TypeVar("_T")
_R = TypeVar("_R")


def error_text(t: Translator, error: Exception, fallback: str) -> str:
    """Translate a known engine error while keeping internal details out of the UI."""
    if isinstance(error, NimboError):
        key = f"reason.{error.code}"
        if t.has(key):
            return t(key)
    return t(fallback)


def key_label(key: str, label: str) -> Text:
    """Set apart a clickable shortcut key without adding a second color."""
    return Text.assemble((key, "bold"), " ", label)


class FlatButton(Button):
    """Use a regular Textual button with a single unpadded line of text."""

    def __init__(self, label: str | Text, *, id: str, classes: str, disabled: bool = False) -> None:
        super().__init__(label, id=id, classes=classes, disabled=disabled)
        self.styles.line_pad = 0
        self.active_effect_duration = 0
        self.set_label(label)

    def set_label(self, label: str | Text) -> None:
        """Size to terminal cells so the label and its shortcuts fit without padding."""
        content = Content.from_text(label)
        self.label = content
        self.styles.width = content.cell_length


class Panel(Screen[_R]):
    """A full-screen view with a localized back action and shared entrance motion."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "back", show=False)]
    AUTO_FOCUS = ""

    @property
    def nimbo(self) -> NimboApp:
        """Return the dashboard app and its injected dependencies."""
        return cast("NimboApp", self.app)

    @property
    def t(self) -> Translator:
        """Access the current translator."""
        return self.nimbo.t

    def heading(self, key: str, description: str | None = None) -> Widget:
        """Create the screen title and optional context in reading order."""
        title = Static(self.t(key), classes="screen-title", markup=False)
        if description is None:
            return title
        return Vertical(
            title,
            Static(description, classes="screen-intro muted", markup=False),
            classes="screen-heading",
        )

    def footer(self, *actions: Button) -> Horizontal:
        """Place navigation and any primary actions on a docked row."""
        return Horizontal(
            FlatButton(key_label("esc", self.t("tui.action.back")), id="panel-back", classes="key"),
            *actions,
            classes="panel-footer",
        )

    def on_mount(self) -> None:
        """Play a short entrance fade on screens that opt in with a body."""
        motion = self.nimbo.motion
        bodies = self.query(".panel-body").nodes
        if not bodies or motion.reduced:
            return
        body = bodies[0]
        self._fade_body = body
        self._fade_started = motion.clock()
        body.styles.opacity = 0.65
        motion.track(self._fade)

    def on_unmount(self) -> None:
        """Release any entrance animation when leaving the screen."""
        self.nimbo.motion.forget(self._fade)

    def _fade(self, now: float) -> bool:
        if not self.is_current:
            self._fade_body.styles.opacity = 1.0
            return False
        fraction = min((now - self._fade_started) / 0.15, 1.0)
        self._fade_body.styles.opacity = 0.65 + 0.35 * (1 - (1 - fraction) ** 2)
        return fraction < 1

    def on_resize(self, event: events.Resize) -> None:
        """Reduce vertical spacing and meter widths on smaller terminals."""
        self.set_class(event.size.height < 29, "short")
        self.set_class(event.size.width < 96, "narrow")

    def action_back(self) -> None:
        """Return to the previous view if there is one."""
        if len(self.app.screen_stack) > 1:
            self.app.pop_screen()

    @on(Button.Pressed, "#panel-back")
    def back_button(self) -> None:
        """Make the back action available to the mouse."""
        self.action_back()

    def start_job(
        self,
        operation: Callable[[Event], _T],
        success: Callable[[_T], None],
        failure: Callable[[Exception], None],
    ) -> Worker[None]:
        """Run blocking I/O off the UI thread and deliver the result on the UI thread."""

        async def job() -> None:
            stop = get_current_worker().cancelled_event
            try:
                result = await self.nimbo.run_io(partial(operation, stop))
            except Exception as exc:  # noqa: BLE001 - worker failures belong in the current view
                if self.is_mounted:
                    failure(exc)
            else:
                if self.is_mounted:
                    success(result)

        return self.run_worker(job)
