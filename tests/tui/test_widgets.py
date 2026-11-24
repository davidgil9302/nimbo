"""Widget rendering and motion run through Textual's normal mount lifecycle."""

import asyncio

from textual.app import App, ComposeResult

from nimbo.app.tui.theme import ATTENTION, Motion
from nimbo.app.tui.widgets.countup import CountUp
from nimbo.app.tui.widgets.pulse import PulseDot
from nimbo.app.tui.widgets.smoothbar import SmoothBar, bar_text


class WidgetApp(App[None]):
    def __init__(self, reduced: bool):
        super().__init__()
        self.clock = 0.0
        self.motion = Motion(self, reduced=reduced, clock=lambda: self.clock)

    def compose(self) -> ComposeResult:
        yield SmoothBar(self.motion)
        yield CountUp(self.motion, formatter=lambda n: str(round(n)))
        yield PulseDot(self.motion, "up_to_date")


def test_smoothbar_half_cells_render_with_one_continuous_track():
    mixed = bar_text(5, 0.675, color=ATTENTION)
    assert mixed.plain == "━━━╾─"
    assert mixed.style == ATTENTION
    assert [(span.start, span.end, span.style) for span in mixed.spans] == [(4, 5, "default dim")]
    assert bar_text(1, 0.5).plain == "╾"
    assert bar_text(98, 0.98).plain == "━" * 96 + "──"
    assert bar_text(6, -1).plain == "─" * 6
    assert bar_text(6, 10).plain == "━" * 6
    assert bar_text(0, 0.5).cell_len == 0


def test_smoothbar_reduced_motion_jumps():
    async def run():
        app = WidgetApp(True)
        async with app.run_test() as pilot:
            bar = app.query_one(SmoothBar)
            bar.value = 0.625
            await pilot.pause()
            assert bar.display_value == 0.625
            assert not app.motion.active

    asyncio.run(run())


def test_smoothbar_tweens_the_thin_fill():
    async def run():
        app = WidgetApp(False)
        async with app.run_test() as pilot:
            bar = app.query_one(SmoothBar)
            empty = bar.render().plain
            bar.value = 1
            await pilot.pause()
            app.clock = 0.1
            app.motion.tick()
            partly_filled = bar.render().plain
            assert "━" in partly_filled and "─" in partly_filled
            assert len(empty) == len(partly_filled)
            app.clock = 0.3
            app.motion.tick()
            assert bar.render().plain == "━" * len(empty)
            assert not app.motion.active

    asyncio.run(run())


def test_countup_tweens():
    async def run():
        app = WidgetApp(False)
        async with app.run_test() as pilot:
            count = app.query_one(CountUp)
            count.value = 80
            await pilot.pause()
            assert app.motion.active
            app.clock = 0.2
            app.motion.tick()
            assert 0 < count.display_value < 80
            app.clock = 0.41
            app.motion.tick()
            assert count.display_value == 80
            assert not app.motion.active

    asyncio.run(run())


def test_pulse_dot_states():
    async def run():
        app = WidgetApp(False)
        async with app.run_test() as pilot:
            dot = app.query_one(PulseDot)
            assert dot.render().plain == "●"
            dot.state = "running"
            await pilot.pause()
            assert app.motion.active
            first = dot.render()
            app.clock = 0.5
            app.motion.tick()
            assert first.plain != dot.render().plain
            assert first.style != dot.render().style
            dot.state = "no_disk"
            await pilot.pause()
            assert dot.render().plain == "○"
            assert not app.motion.active

    asyncio.run(run())
