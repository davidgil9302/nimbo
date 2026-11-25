"""Use the pilot to exercise navigation and operations without external services."""

import asyncio
import re
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from rich.cells import cell_len
from textual.widgets import Input, Static, Tree

from nimbo.app.tui.screens.confirm import ConfirmScreen
from nimbo.app.tui.screens.dashboard import DashboardScreen
from nimbo.app.tui.screens.diagnostics import DiagnosticsScreen
from nimbo.app.tui.screens.history import HistoryScreen, RunDetailScreen, UndoScreen
from nimbo.app.tui.screens.pending import PendingScreen
from nimbo.app.tui.screens.settings import SettingsScreen
from nimbo.app.tui.widgets.recent import RecentBackups
from nimbo.app.tui.widgets.smoothbar import SmoothBar
from nimbo.core.config import load_config, save_config
from nimbo.core.fsutil import read_json
from nimbo.core.index import Index
from nimbo.core.lock import RunLock
from nimbo.core.model import (
    BrakeDecision,
    FolderStat,
    Op,
    OpKind,
    Plan,
    PlanStats,
    Preview,
    RunResult,
)
from nimbo.core.safety import OVERWRITE_CONFIRMATION


async def settle(pilot):
    """Wait for the pilot and the outstanding I/O at this interaction boundary."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_keys_trigger_actions(dashboard, scenario):
    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("r")
            await settle(pilot)
            assert scenario.starts == 1
            scenario.track()
            app.refresh_snapshot()
            await settle(pilot)
            await pilot.press("x")
            await settle(pilot)
            assert scenario.stops == 1
            await pilot.press("c")
            await settle(pilot)
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape", "d")
            await settle(pilot)
            assert isinstance(app.screen, DiagnosticsScreen)
            assert "backup diagnostico" in app.screen.query_one("#diagnostics-body", Static).content

    asyncio.run(run())


@pytest.mark.parametrize("language", ["es", "en"])
@pytest.mark.parametrize("state", ["idle", "running", "confirmation"])
def test_compact_footer_renders_every_label(dashboard, scenario, language, state):
    if state == "running":
        scenario.track()
    if state == "confirmation":
        scenario.record_run(
            RunResult.NEEDS_CONFIRMATION, fingerprint="test", trash_preview=["Projects/Old\t1"]
        )
    labels = {
        "es": ("p Pendientes", "h Historial", "c Ajustes", "d Diagnóstico", "q Salir"),
        "en": ("p Pending", "h History", "c Settings", "d Diagnostics", "q Quit"),
    }
    first = {
        "es": {"idle": "r Respaldar", "running": "x Detener", "confirmation": "r Revisar"},
        "en": {"idle": "r Back up", "running": "x Stop", "confirmation": "r Review"},
    }

    async def run():
        app = dashboard(language)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            footer = app.screen.query_one("#dashboard-footer")
            line = app.screen._compositor.render_strips()[footer.region.y].text
            assert re.split(r" {2,}", line.strip()) == [first[language][state], *labels[language]]
            assert await pilot.click("#nav-diagnostics")
            await settle(pilot)
            assert isinstance(app.screen, DiagnosticsScreen)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("size", "visible"), [((80, 24), False), ((100, 32), False), ((120, 40), True)]
)
def test_recent_backups_use_the_space_in_tall_terminals(dashboard, size, visible):
    async def run():
        app = dashboard()
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            screen = app.screen
            content = screen.query_one("#dashboard-content")
            actions = screen.query_one("#dashboard-actions")
            assert content.region.width == actions.region.width <= 98
            assert content.region.x == actions.region.x
            recent = screen.query_one(RecentBackups)
            assert recent.display is visible
            if visible:
                assert recent.region.bottom < actions.region.y
                assert recent.query_one(".recent-files", Static).content == "9 files"

    asyncio.run(run())


@pytest.mark.parametrize("size", [(80, 24), (100, 32), (120, 40)])
def test_dashboard_meters_fill_the_row_without_wrapping_names(dashboard, scenario, size):
    async def run():
        app = dashboard("es")
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            screen = app.screen
            quota = screen.query_one("#quota-bar", SmoothBar)
            assert quota.region.width == quota.parent.content_size.width

            scenario.track()
            app.refresh_snapshot()
            await settle(pilot)
            name = screen.query_one(".transfer-name", Static)
            name.update("A very long filename " * 4 + ".pdf")
            await pilot.pause()

            bars = [
                bar
                for bar in screen.query(".folder-row .mini-bar, .transfer-row .mini-bar")
                if bar.visible
            ]
            assert len(bars) == 6
            assert len({bar.region.x for bar in bars}) == 1
            transfer_bars = screen.query(".transfer-row .mini-bar")
            assert len({bar.region.right for bar in transfer_bars}) == 1
            for bar in bars:
                figures = bar.parent.query_one(".folder-state, .transfer-figures")
                assert 8 <= bar.region.width <= 40
                assert 2 <= figures.region.x - bar.region.right <= 10
            frame = screen._compositor.render_strips()
            assert "…" in frame[name.region.y].text
            assert name.region.height == 1

    asyncio.run(run())


@pytest.mark.parametrize(("size", "language"), [((80, 24), "es"), ((120, 40), "en")])
def test_folder_bars_share_start_and_end_columns(dashboard, size, language):
    async def run():
        app = dashboard(language)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            screen = app.screen
            bars = list(screen.query(".folder-row .mini-bar"))
            shown = [bar for bar in bars if bar.visible]
            assert len(shown) == 3
            assert len({(bar.region.x, bar.region.right) for bar in shown}) == 1
            states = list(screen.query(".folder-state").results(Static))
            widest = max(cell_len(state.content) for state in states)
            assert {state.region.width for state in states} == {widest}
            assert len({state.region.right for state in states}) == 1
            frame = screen._compositor.render_strips()
            for state in states:
                region = state.region
                assert frame[region.y].text[region.x : region.right].endswith(state.content)
            paused = next(bar for bar in bars if not bar.visible)
            row = frame[paused.parent.region.y].text
            bar_column = shown[0].region
            assert not row[bar_column.x : bar_column.right].strip()

    asyncio.run(run())


def test_folder_state_width_keeps_room_for_the_bar_when_resized(dashboard, scenario):
    source = replace(scenario.cfg.source, paused=("Projects", "Archive"))
    save_config(replace(scenario.cfg, source=source), scenario.paths.config_file)
    with closing(Index(scenario.paths.db_file)) as index:
        index.put_folder_stats(
            [FolderStat("Documents", files_pending=2**62, bytes_pending=4_000_000_000_000_000_000)]
        )

    async def run():
        app = dashboard("es")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            state = next(
                s for s in screen.query(".folder-state").results(Static) if "cambios" in s.content
            )
            assert state.region.width == cell_len(state.content)
            await pilot.resize_terminal(80, 24)
            await settle(pilot)
            assert state.region.width < cell_len(state.content)
            bars = [bar for bar in screen.query(".folder-row .mini-bar") if bar.visible]
            assert len(bars) == 2
            assert {bar.region.width for bar in bars} == {8}
            assert "…" in screen._compositor.render_strips()[state.region.y].text
            await pilot.resize_terminal(120, 40)
            await settle(pilot)
            assert state.region.width == cell_len(state.content)

    asyncio.run(run())


def test_pending_screen_worker_and_locked_message(dashboard, scenario):
    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("p")
            await settle(pilot)
            assert isinstance(app.screen, PendingScreen)
            assert scenario.previews == scenario.closes == 1
            tree = app.screen.query_one(Tree)
            assert len(tree.root.children) == 3
            await pilot.click("#pending-filter")
            await pilot.press("m", "a", "p")
            await pilot.pause()
            assert len(tree.root.children) == 1
            await pilot.press("escape")
            with RunLock(scenario.paths.lock_file):
                await pilot.press("p")
                await settle(pilot)
                assert scenario.previews == 1
                status = app.screen.query_one("#pending-status", Static)
                assert "backup is running" in status.content

    asyncio.run(run())


def test_history_undo_flow(dashboard, scenario):
    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            assert isinstance(app.screen, HistoryScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.click("#undo-run")
            assert isinstance(app.screen, UndoScreen)
            assert scenario.undone is None
            await pilot.click("#undo-confirm")
            await settle(pilot)
            assert isinstance(app.screen, RunDetailScreen)
            assert scenario.undone == 1 and scenario.closes == 1

    asyncio.run(run())


def test_settings_validation_and_save(dashboard, scenario):
    async def run():
        app = dashboard("es")
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await settle(pilot)
            assert isinstance(app.screen, SettingsScreen)
            screen = app.screen
            screen.query_one("#daily-at", Input).value = "24:00"
            await pilot.click("#settings-save")
            await settle(pilot)
            assert "HH:MM" in screen.query_one("#settings-status", Static).content
            assert scenario.installed_at is None
            screen.query_one("#daily-at", Input).value = "07:45"
            screen.query_one("#max-deletes", Input).value = "120"
            screen.query_one("#max-ratio", Input).value = "12,5"
            await pilot.click("#folder-0")
            await pilot.click("#settings-save")
            await settle(pilot)
            cfg = load_config(scenario.paths.config_file)
            assert cfg.schedule.daily_at == scenario.installed_at == "07:45"
            assert cfg.safety.max_deletes == 120 and cfg.safety.max_delete_ratio == 0.125
            assert cfg.source.paused == ("Projects", "Archive")

    asyncio.run(run())


def test_settings_save_preserves_overwrite_thresholds(dashboard, scenario):
    safety = replace(
        scenario.cfg.safety, max_updates=7, max_update_ratio=0.2, update_ratio_min_files=5
    )
    save_config(replace(scenario.cfg, safety=safety), scenario.paths.config_file)

    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("c")
            await settle(pilot)
            assert isinstance(app.screen, SettingsScreen)
            app.screen.query_one("#max-deletes", Input).value = "120"
            assert await pilot.click("#settings-save")
            await settle(pilot)
            saved = load_config(scenario.paths.config_file)
            assert saved.safety == replace(safety, max_deletes=120)

    asyncio.run(run())


@pytest.mark.parametrize("overwrites", [False, True], ids=["deletions", "overwrites"])
def test_confirm_screen_writes_approval_and_starts(dashboard, scenario, monkeypatch, overwrites):
    if overwrites:
        plan = Plan([Op(OpKind.UPDATE, "Documents/Field notes.pdf", "changed")], PlanStats())
        preview = Preview(plan, [], None, BrakeDecision(True, ("too_many_updates",), "test"))
        monkeypatch.setattr(scenario, "preview", lambda *, stop: preview)

    async def run():
        scenario.record_run(
            RunResult.NEEDS_CONFIRMATION,
            fingerprint="test",
            reason=OVERWRITE_CONFIRMATION if overwrites else "",
            trash_preview=[] if overwrites else ["Projects/Archive\t18"],
        )
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            if overwrites:
                rows = app.screen.query_one(".deletion-row", Static).render().plain
                assert rows == "Documents/Field notes.pdf · overwrite in Drive"
            assert not scenario.paths.approval_file.exists()
            await pilot.click("#confirm-start")
            await settle(pilot)
            assert scenario.starts == 1
            assert read_json(scenario.paths.approval_file)["fingerprint"] == "test"

    asyncio.run(run())


def test_quit(dashboard):
    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("q")

        assert app.return_value == 0

    asyncio.run(run())


def test_dashboard_reports_damaged_run_in_spanish_and_exits_with_error(dashboard, scenario):
    with sqlite3.connect(scenario.paths.db_file) as db:
        db.execute("UPDATE run SET summary = ?", ('{"result":"ok","failed":null}',))

    async def run():
        app = dashboard("es")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert (
                app.t("tui.error.index")
                in app.screen.query_one("#dashboard-notice", Static).content
            )
            await pilot.press("q")
        assert app.return_code == 1

    asyncio.run(run())
