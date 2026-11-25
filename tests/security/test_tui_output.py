"""Exercise literal Drive names and deletion approval through the headless dashboard."""

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Button, Static
from textual.widgets._tooltip import Tooltip

from nimbo.app.context import safe_label
from nimbo.app.tui.screens.confirm import ConfirmScreen
from nimbo.app.tui.screens.dashboard import DashboardScreen
from nimbo.core.config import save_config
from nimbo.core.fsutil import read_json
from nimbo.core.model import (
    BrakeDecision,
    Op,
    OpKind,
    Plan,
    PlanIssue,
    PlanStats,
    Preview,
    RunResult,
)
from nimbo.core.progress import ProgressTracker
from tests.tui import conftest as tui_fixtures
from tests.tui.conftest import NOW
from tests.tui.test_app import settle

dashboard = tui_fixtures.dashboard
scenario = tui_fixtures.scenario


def _track(scenario, path):
    scenario.running = True
    tracker = ProgressTracker(scenario.paths.progress_file, 20, wall=NOW.isoformat)
    tracker.phase("transfer", ops_total=1, bytes_total=2, folder="Documents")
    tracker.transfer(path, 1, 2)
    tracker.flush(force=True)


@pytest.mark.parametrize(
    ("selector", "label", "path"),
    [
        (".transfer-name", "[b]Report.pdf", "Documents/[b]Report.pdf"),
        (".folder-name", "[b]Reports", "[b]Reports"),
    ],
)
def test_dashboard_lists_and_tooltips_show_markup_literally(
    dashboard, scenario, selector, label, path
):
    if selector == ".folder-name":
        source = replace(scenario.cfg.source, folders=(label, *scenario.cfg.source.folders))
        (source.root / label).mkdir()
        save_config(replace(scenario.cfg, source=source), scenario.paths.config_file)
    else:
        _track(scenario, path)

    async def run():
        app = dashboard()
        app.TOOLTIP_DELAY = 0.01
        async with app.run_test(size=(100, 32), tooltips=True) as pilot:
            await settle(pilot)
            widget = app.screen.query(selector).first(Static)
            assert widget.render().plain == label
            assert await pilot.hover(widget)
            await pilot.pause(0.1)
            tooltip = app.screen.query_one(Tooltip)
            assert tooltip.display
            assert tooltip.render().plain == path

    asyncio.run(run())


def test_hovering_transfer_with_unbalanced_closing_tag_keeps_dashboard_open(dashboard, scenario):
    path = "Documents/x[/oops].txt"
    _track(scenario, path)

    async def run():
        app = dashboard()
        app.TOOLTIP_DELAY = 0.01
        async with app.run_test(size=(100, 32), tooltips=True) as pilot:
            await settle(pilot)
            assert await pilot.hover(".transfer-name")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DashboardScreen)
            tooltip = app.screen.query_one(Tooltip)
            assert tooltip.display
            assert tooltip.render().plain == path

    asyncio.run(run())


def test_deletion_review_escapes_format_characters_and_keeps_markup_literal(dashboard, scenario):
    path = "Documents/[b]Report[/b]\u202e\u2066\u2067\u2068\u2069\u200b\u2028\u2029\t.pdf"
    scenario.record_run(
        RunResult.NEEDS_CONFIRMATION,
        fingerprint="test",
        trash_preview=[f"{path}\t1"],
    )

    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            assert app.screen.query_one(".deletion-row", Static).render().plain == (
                f"{safe_label(path)} · 1 file"
            )

    asyncio.run(run())


def _large_plan(scenario, *, changed=False, has_errors=False):
    paths = [f"Documents/file-{i:03}.pdf" for i in range(501)]
    paths[-1] = "Documents/z [bold]last[/bold].pdf"
    scenario.record_run(
        RunResult.NEEDS_CONFIRMATION,
        fingerprint="reviewed",
        trash_preview=[f"{path}\t1" for path in paths[:500]],
    )
    plan = Plan(
        [Op(OpKind.TRASH, path, "deleted-locally", affected_files=1) for path in reversed(paths)],
        PlanStats(trashes=len(paths), trashed_files=len(paths)),
        errors=[PlanIssue("source_not_mounted", "")] if has_errors else [],
    )
    fingerprint = "changed" if changed else "reviewed"
    return paths, Preview(plan, [], None, BrakeDecision(True, (), fingerprint))


def test_large_deletion_review_shows_total_and_every_item_before_approval(
    dashboard, scenario, monkeypatch
):
    paths, preview = _large_plan(scenario)
    monkeypatch.setattr(scenario, "preview", lambda *, stop: preview)

    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            assert "501" in app.screen.query_one("#confirm-total", Static).render().plain
            rendered = "\n".join(
                row.render().plain for row in app.screen.query(".deletion-row").results(Static)
            )
            assert [row for row in rendered.splitlines() if row] == [
                f"{path} · 1 file" for path in paths
            ]
            assert scenario.closes == 1
            assert scenario.starts == 0
            assert not scenario.paths.approval_file.exists()
            assert await pilot.click("#confirm-start")
            await settle(pilot)
            assert scenario.starts == 1
            assert read_json(scenario.paths.approval_file)["fingerprint"] == "reviewed"

    asyncio.run(run())


@pytest.mark.parametrize("has_errors", [False, True], ids=["changed", "errors"])
def test_large_deletion_review_rejects_an_unreviewable_plan(
    dashboard, scenario, monkeypatch, has_errors
):
    _, preview = _large_plan(scenario, changed=not has_errors, has_errors=has_errors)
    monkeypatch.setattr(scenario, "preview", lambda *, stop: preview)

    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            await pilot.click("#confirm-start")
            await settle(pilot)
            assert scenario.starts == 0
            assert not scenario.paths.approval_file.exists()
            assert app.screen.query_one("#confirm-start", Button).disabled
            assert scenario.closes == 1

    asyncio.run(run())


def test_deletion_approval_rechecks_the_configuration(dashboard, scenario):
    scenario.record_run(
        RunResult.NEEDS_CONFIRMATION,
        fingerprint="test",
        trash_preview=["Projects/Archive\t18"],
    )

    async def run():
        app = dashboard()
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert not app.screen.query_one("#confirm-start", Button).disabled
            save_config(
                replace(
                    scenario.cfg, target=replace(scenario.cfg.target, folder_id="other_test_folder")
                ),
                scenario.paths.config_file,
            )
            await pilot.click("#confirm-start")
            await settle(pilot)
            assert scenario.starts == 0
            assert not scenario.paths.approval_file.exists()
            assert app.screen.query_one("#confirm-start", Button).disabled

    asyncio.run(run())
