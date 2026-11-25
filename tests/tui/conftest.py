"""Isolated dashboard scenarios backed by the real config, index and progress files."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from nimbo.app.service import ServiceState
from nimbo.core.config import Config, OAuthConfig, SourceConfig, TargetConfig, save_config
from nimbo.core.index import META_QUOTA_LIMIT, META_QUOTA_USED, Index
from nimbo.core.model import (
    BrakeDecision,
    FolderStat,
    Op,
    OpKind,
    Plan,
    PlanStats,
    Preview,
    RunResult,
    RunSummary,
    UndoSummary,
)
from nimbo.core.paths import Paths
from nimbo.core.progress import ProgressTracker

NOW = datetime(2026, 11, 23, 20, 30, tzinfo=UTC)
MB = 1024**2
GB = 1024**3


@dataclass
class Scenario:
    paths: Paths
    cfg: Config
    running: bool = False
    starts: int = 0
    stops: int = 0
    previews: int = 0
    closes: int = 0
    undone: int | None = None
    installed_at: str | None = None

    def service_state(self) -> ServiceState:
        return ServiceState(True, True, self.running, NOW + timedelta(days=1, minutes=-30))

    def start(self, paths: Paths) -> None:
        assert paths == self.paths
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def install(self, exe: Path, daily_at: str) -> None:
        self.installed_at = daily_at

    def engine(self):
        return self, self.close

    def close(self) -> None:
        self.closes += 1

    def preview(self, *, stop: Event | None = None) -> Preview:
        assert stop is not None
        self.previews += 1
        ops = [
            Op(OpKind.UPDATE, "Documents/Field notes.pdf", "changed", size=12 * MB),
            Op(OpKind.UPLOAD, "Documents/Maps/Trail map.png", "new", size=2 * MB),
            Op(OpKind.MOVE, "Photos/Summer.jpg", "renamed", src_rel="Photos/Trip.jpg"),
            Op(OpKind.TRASH, "Projects/Archive", "deleted-locally", affected_files=18),
        ]
        return Preview(Plan(ops, PlanStats()), [], None, BrakeDecision(False, (), ""))

    def undo(self, run_id: int | None = None, *, stop: Event | None = None) -> UndoSummary:
        assert stop is not None
        self.undone = run_id
        return UndoSummary(run_id, untrashed=2, restored_files=1, skipped_existing=["Report.pdf"])

    def record_run(
        self, result: RunResult = RunResult.OK, *, at: datetime | None = None, **kwargs
    ) -> RunSummary:
        index = Index(self.paths.db_file)
        at = at or NOW.replace(hour=20, minute=0)
        started = at.isoformat()
        finished = (at + timedelta(minutes=3)).isoformat()
        try:
            summary = RunSummary(
                index.start_run("manual", started),
                result,
                started_at=started,
                finished_at=finished,
                trigger="manual",
                **kwargs,
            )
            index.finish_run(summary)
            return summary
        finally:
            index.close()

    def track(self) -> None:
        self.running = True
        ticks = [0.0]
        tracker = ProgressTracker(
            self.paths.progress_file, 20, clock=lambda: ticks[0], wall=NOW.isoformat
        )
        tracker.phase("transfer", ops_total=18, bytes_total=850 * MB, folder="Documents")
        for amount in (42, 38, 70, 49, 72, 50, 36, 55, 49):
            ticks[0] += 5
            tracker.transfer("Documents/Processed/Album.zip", amount * MB, 720 * MB)
        tracker.transfer("Documents/Field notes.pdf", 18 * MB, 22 * MB)
        tracker.transfer("Documents/Survey footage.zip", 115 * MB, 330 * MB)
        tracker.flush(force=True)


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.ensure()
    root = tmp_path / "source"
    folders = ("Projects", "Photos", "Documents", "Archive")
    for name in folders:
        (root / name).mkdir(parents=True)
    cfg = Config(
        SourceConfig(root, folders, paused=("Archive",), require_mount=False),
        TargetConfig("test_folder_id", "Workstation \u203a Files"),
        OAuthConfig("test.apps.googleusercontent.com", "test-only"),
    )
    save_config(cfg, paths.config_file)
    index = Index(paths.db_file)
    try:
        index.put_folder_stats(
            [
                FolderStat("Projects", NOW.isoformat(), 74, 3 * GB, 0, 0),
                FolderStat("Photos", NOW.isoformat(), 399, 5 * GB, 0, 0),
                FolderStat("Documents", NOW.isoformat(), 36, 700 * MB, 3, 177 * MB),
            ]
        )
        index.set_meta(META_QUOTA_USED, str(249 * GB))
        index.set_meta(META_QUOTA_LIMIT, str(400 * GB))
    finally:
        index.close()
    data = Scenario(paths, cfg)
    data.record_run(uploaded=8, uploaded_bytes=7 * MB, updated=1, trashed=2)
    return data


@pytest.fixture
def dashboard(scenario: Scenario):
    from nimbo.app.tui.app import NimboApp

    def build(language: str = "en", **kwargs) -> NimboApp:
        options = {
            "reduced_motion": True,
            "now": lambda: NOW,
            "service_state": scenario.service_state,
            "start_backup": scenario.start,
            "stop_backup": scenario.stop,
            "install_units": scenario.install,
            "executable": scenario.paths.config_dir / "test-launcher",
            "engine_factory": scenario.engine,
        }
        return NimboApp(scenario.paths, language=language, **(options | kwargs))

    return build
