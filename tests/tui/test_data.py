"""The dashboard combines its read-only inputs without failing closed."""

from dataclasses import replace
from pathlib import Path

from nimbo.app.tui.data import load_snapshot
from nimbo.core.config import save_config
from nimbo.core.index import Index
from nimbo.core.model import FolderStat, RunResult
from nimbo.core.paths import Paths

from .conftest import GB, MB, NOW


def _load(scenario):
    return load_snapshot(scenario.paths, service_state=scenario.service_state, now=lambda: NOW)


def test_snapshot_unconfigured(tmp_path: Path):
    snapshot = load_snapshot(Paths(tmp_path, tmp_path), now=lambda: NOW)
    assert not snapshot.configured
    assert snapshot.errors == ()
    assert snapshot.progress is None


def test_snapshot_idle_with_history(scenario):
    snapshot = _load(scenario)
    assert snapshot.configured
    assert snapshot.service == "Google Drive"
    assert snapshot.target_label == "Workstation \u203a Files"
    assert snapshot.last_run and snapshot.last_run.uploaded == 8
    assert snapshot.recent_runs == (snapshot.last_run,)
    assert snapshot.next_run == scenario.service_state().next_run
    assert (snapshot.quota_used, snapshot.quota_total) == (249 * GB, 400 * GB)
    assert [folder.state for folder in snapshot.folders] == [
        "up_to_date",
        "up_to_date",
        "changes",
        "paused",
    ]


def test_snapshot_running_progress(scenario):
    scenario.track()
    snapshot = _load(scenario)
    assert snapshot.progress and snapshot.progress.active
    assert snapshot.running and snapshot.progress.phase == "transfer"
    assert snapshot.folders[2].state == "running"
    assert snapshot.folders[0].state == "up_to_date"


def test_snapshot_needs_confirmation(scenario):
    scenario.record_run(
        RunResult.NEEDS_CONFIRMATION, fingerprint="abc", trash_preview=["Projects/Archive\t18"]
    )
    assert _load(scenario).needs_confirmation
    scenario.track()
    assert not _load(scenario).needs_confirmation


def test_snapshot_never_raises_on_corrupt_db_or_progress(scenario):
    scenario.paths.db_file.write_bytes(b"not SQLite")
    scenario.paths.progress_file.write_bytes(b'{"phase":')
    snapshot = _load(scenario)
    assert snapshot.configured and snapshot.progress is None
    assert set(snapshot.errors) == {"tui.error.index", "tui.error.progress"}
    assert snapshot.folders[0].state == "unknown"
    assert snapshot.recent_runs == ()


def test_recent_backups_are_bounded_and_newest_first(scenario):
    runs = [scenario.record_run(uploaded=count) for count in range(7)]
    snapshot = _load(scenario)
    assert snapshot.last_run == runs[-1]
    assert snapshot.recent_runs == tuple(reversed(runs[-4:]))


def test_coverage_math(scenario):
    snapshot = _load(scenario)
    assert snapshot.folders[2].coverage == (700 - 177) / 700
    assert snapshot.coverage == (8 * GB + (700 - 177) * MB) / (8 * GB + 700 * MB)
    index = Index(scenario.paths.db_file)
    index.put_folder_stats([FolderStat("Documents", files_pending=2, bytes_pending=3)])
    index.close()
    assert _load(scenario).folders[2].coverage == 0
    source = replace(scenario.cfg.source, require_mount=True)
    save_config(replace(scenario.cfg, source=source), scenario.paths.config_file)
    assert _load(scenario).folders[2].state == "no_disk"
