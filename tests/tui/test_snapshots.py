"""Golden SVGs for the bilingual dashboard and its primary navigation routes."""

from datetime import timedelta

import pytest
from pytest_textual_snapshot import SVGImageExtension

from nimbo.core.model import RunResult

from .conftest import MB, NOW
from .test_app import settle


@pytest.mark.parametrize(
    ("view", "language", "size"),
    [
        pytest.param("idle", "es", (100, 32), id="idle-es"),
        pytest.param("idle", "en", (120, 40), id="idle-en-wide"),
        pytest.param("running", "es", (100, 32), id="running-es"),
        pytest.param("running", "en", (80, 24), id="running-en-compact"),
        pytest.param("confirmation", "en", (100, 32), id="confirmation-en"),
        pytest.param("idle", "es", (80, 24), id="idle-es-compact"),
        pytest.param("pending", "es", (100, 32), id="pending-es"),
        pytest.param("history", "en", (100, 32), id="history-en"),
        pytest.param("welcome", "es", (80, 24), id="welcome-es"),
    ],
)
def test_dashboard_snapshot(snap_compare, dashboard, scenario, view, language, size, monkeypatch):
    # Syrupy 7 renamed the field used by pytest-textual-snapshot 1.0.
    if hasattr(SVGImageExtension, "file_extension"):
        monkeypatch.setattr(SVGImageExtension, "file_extension", "svg")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if size == (120, 40):
        nightly = NOW.replace(minute=0)
        scenario.record_run(RunResult.FAILED, at=nightly - timedelta(days=3), reason="auth_failed")
        scenario.record_run(at=nightly - timedelta(days=2), uploaded=14, uploaded_bytes=53 * MB)
        scenario.record_run(
            RunResult.PARTIAL,
            at=nightly - timedelta(days=1),
            updated=2,
            uploaded_bytes=21 * MB,
        )
        scenario.record_run(at=nightly, uploaded=8, updated=1, uploaded_bytes=7 * MB)
    if view == "running":
        scenario.track()
    if view == "history":
        scenario.record_run(
            RunResult.PARTIAL, uploaded=2, failed=[("Photos/Trail.jpg", "file_in_use")]
        )
        scenario.record_run(RunResult.SKIPPED, reason="source_not_mounted")
    if view == "confirmation":
        scenario.record_run(
            RunResult.NEEDS_CONFIRMATION,
            fingerprint="test",
            trash_preview=["Projects/Archive\t18"],
        )
    if view == "welcome":
        scenario.paths.config_file.unlink()

    async def prepare(pilot):
        await settle(pilot)
        key = {"pending": "p", "history": "h"}.get(view)
        if key:
            await pilot.press(key)
            await settle(pilot)

    assert snap_compare(dashboard(language), terminal_size=size, run_before=prepare)
