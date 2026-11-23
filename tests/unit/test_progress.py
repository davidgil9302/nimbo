from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nimbo.core import progress
from nimbo.core.fsutil import write_json
from nimbo.core.model import ActiveTransfer, Progress


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_throttled_atomic_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "progress.json"
    clock = _Clock()
    real_write = progress.atomic_write_text
    writes = []

    def write(target: Path, text: str) -> None:
        assert target == path
        real_write(target, text)
        writes.append((clock.now, Progress.from_json(text)))

    monkeypatch.setattr(progress, "atomic_write_text", write)
    tracker = progress.ProgressTracker(path, 7, clock=clock, wall=lambda: "2026-11-23T00:00:00Z")
    tracker.phase("transfer", ops_total=2, bytes_total=100)
    original = path.read_text()
    clock.now = 0.25
    tracker.transfer("Projects/a", 10, 25)
    tracker.flush()
    assert path.read_text() == original
    assert tracker.snapshot().bytes_done == 10
    clock.now = 0.5
    tracker.flush()
    clock.now = 0.75
    tracker.op_done("Projects/a")
    tracker.flush(force=True)
    assert [time for time, _ in writes] == [0, 0.5, 0.75]
    assert progress.read_progress(path) == writes[-1][1]
    assert writes[-1][1].ops_done == 1
    assert writes[-1][1].active == []
    assert writes[-1][1].started_at == "2026-11-23T00:00:00Z"


def test_speed_eta_and_history(tmp_path: Path) -> None:
    clock = _Clock()
    tracker = progress.ProgressTracker(tmp_path / "progress.json", 1, clock=clock, min_interval=100)
    tracker.phase("transfer", bytes_total=901)
    tracker.transfer("Photos/a", 50, 901)
    assert tracker.snapshot().speed_bps == 0
    assert tracker.snapshot().eta_s is None
    clock.now = 1
    tracker.transfer("Photos/a", 50, 901)
    first = tracker.snapshot()
    assert first.speed_bps == 100
    assert first.eta_s == 9
    assert first.speed_history == [100]
    clock.now = 2
    tracker.transfer("Photos/a", 200, 901)
    second = tracker.snapshot()
    assert 100 < second.speed_bps < 200
    assert second.eta_s is not None and second.eta_s < first.eta_s
    assert len(second.speed_history) == 2
    clock.now = 10
    assert tracker.snapshot().speed_bps < second.speed_bps
    clock.now = 100
    final = tracker.snapshot()
    assert len(final.speed_history) == 60
    assert final.speed_bps == final.speed_history[-1]
    assert final.speed_history[0] < first.speed_bps


def test_concurrent_transfers_thread_safe(tmp_path: Path) -> None:
    tracker = progress.ProgressTracker(
        tmp_path / "progress.json", 4, clock=lambda: 0, wall=lambda: "2026-11-23T00:00:00Z"
    )
    tracker.phase("transfer", ops_total=4, bytes_total=400)

    def send(i: int) -> None:
        rel = f"Projects/{i}"
        for _ in range(100):
            tracker.transfer(rel, 1, 100)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, range(4)))
    state = tracker.snapshot()
    assert sorted((a.rel, a.done, a.total) for a in state.active) == [
        (f"Projects/{i}", 100, 100) for i in range(4)
    ]
    assert state.bytes_done == 400

    def complete(i: int) -> None:
        tracker.op_done(f"Projects/{i}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(complete, range(4)))
    tracker.flush(force=True)
    assert tracker.snapshot().ops_done == 4
    assert tracker.snapshot().active == []
    assert progress.read_progress(tmp_path / "progress.json") == tracker.snapshot()


def test_folder_precedence_and_detached_snapshots(tmp_path: Path) -> None:
    tracker = progress.ProgressTracker(tmp_path / "progress.json", 1, clock=lambda: 0)
    tracker.phase("transfer", folder="Explicit")
    tracker.transfer("Projects/item", 2, 3)
    tracker.transfer("Photos/item", 1, 4)
    assert tracker.snapshot().folder == "Explicit"
    tracker.phase("transfer")
    assert tracker.snapshot().folder == "Photos"
    tracker.transfer("Projects/item", 1, 3)
    assert tracker.snapshot().folder == "Projects"
    detached = tracker.snapshot()
    detached.active[-1].done = 0
    detached.speed_history.append(999)
    assert tracker.snapshot().active[-1] == ActiveTransfer("Projects/item", 3, 3)
    assert not tracker.snapshot().speed_history
    tracker.transfer_done("Projects/item")
    assert tracker.snapshot().folder == "Photos"
    tracker.transfer_done("Photos/item")
    assert tracker.snapshot().folder is None
    assert tracker.snapshot().ops_done == 0


def test_phase_totals_reset_their_counters(tmp_path: Path) -> None:
    tracker = progress.ProgressTracker(tmp_path / "progress.json", 1, clock=lambda: 0)
    tracker.phase("mkdir", ops_total=1)
    tracker.op_done("Projects")
    tracker.phase("transfer", ops_total=2, bytes_total=10)
    tracker.transfer("Projects/item", 8, 10)
    assert tracker.snapshot().ops_done == 0
    tracker.phase("trash", ops_total=3)
    assert tracker.snapshot().bytes_done == 8
    tracker.phase("transfer", bytes_total=20)
    assert tracker.snapshot().bytes_done == 0


def test_read_progress_dead_pid_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "progress.json"
    tracker = progress.ProgressTracker(path, 1)
    tracker.flush()

    def kill(pid: int, signal: int) -> None:
        assert (pid, signal) == (os.getpid(), 0)
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill)
    assert progress.read_progress(path) is None


def test_read_progress_unowned_live_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "progress.json"
    tracker = progress.ProgressTracker(path, 1)
    tracker.flush()

    def kill(pid: int, signal: int) -> None:
        assert (pid, signal) == (os.getpid(), 0)
        raise PermissionError

    monkeypatch.setattr(os, "kill", kill)
    assert progress.read_progress(path) is not None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pid", 0),
        ("pid", -1),
        ("pid", True),
        ("pid", "10"),
        ("active", {}),
        ("active", [{"rel": "x", "done": 1}]),
        ("speed_history", [float("nan")]),
        ("phase", {}),
    ],
)
def test_read_progress_rejects_invalid_fields(tmp_path: Path, key: str, value: object) -> None:
    path = tmp_path / "progress.json"
    data = {
        "run_id": 1,
        "pid": os.getpid(),
        "phase": "transfer",
        "started_at": "2026-11-23T00:00:00Z",
        "updated_at": "2026-11-23T00:00:01Z",
    }
    write_json(path, {**data, key: value})
    assert progress.read_progress(path) is None


def test_close_removes_file(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    tracker = progress.ProgressTracker(path, 1)
    assert progress.read_progress(path) is None
    path.write_text("{broken")
    assert progress.read_progress(path) is None
    tracker.flush(force=True)
    assert path.exists()
    tracker.close()
    tracker.close()
    tracker.transfer("Projects/late", 1, 1)
    tracker.flush(force=True)
    assert not path.exists()
    next_tracker = progress.ProgressTracker(path, 2)
    next_tracker.flush()
    tracker.close()
    state = progress.read_progress(path)
    assert state is not None and state.run_id == 2
