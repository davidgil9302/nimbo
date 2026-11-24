"""Kill real backup subprocesses while the loopback fake holds a write response."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from threading import Event

import httpx
import pytest

from nimbo.core.config import save_config
from nimbo.core.model import RunSummary
from tests.fakedrive import FaultPlan
from tests.system.helpers import (
    CHUNK_SIZE,
    SMALL_TREE,
    Mirror,
    assert_no_duplicates,
    assert_ok,
    assert_untouched,
    count_writes,
    make_mirror,
    write_calls,
)

pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parents[2]
_CUTS = sorted(Random(810623).sample(range(1, 33), k=20))
_DEADLINE = 40


@dataclass
class _ObservedWrites:
    nth_write: int
    reached: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    writes: int = 0
    chunk_bytes: int = 0
    chunk_ranges: list[tuple[str, str]] = field(default_factory=list)
    cut_status: int | None = None
    cut_range: str | None = None


@contextmanager
def _serve_at_cut(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch, nth_write: int
) -> Iterator[tuple[str, _ObservedWrites]]:
    observed = _ObservedWrites(nth_write)
    handle = mirror.fake._handle

    def observe(request: httpx.Request, base: str) -> httpx.Response:
        byte_range = request.headers.get("Content-Range")
        if request.method == "PUT" and byte_range and request.content:
            observed.chunk_bytes += len(request.content)
            observed.chunk_ranges.append((request.url.params["upload_id"], byte_range))
        response = handle(request, base)
        if FaultPlan.is_write(request):
            observed.writes += 1
            if observed.writes == observed.nth_write:
                observed.cut_status = response.status_code
                observed.cut_range = response.headers.get("Range")
                # The fake has applied the request. Hold the reply until the process is dead.
                observed.reached.set()
                assert observed.release.wait(_DEADLINE * 2), "test did not release HTTP reply"
        return response

    monkeypatch.setattr(mirror.fake, "_handle", observe)
    started = False
    try:
        with mirror.fake.serve() as base:
            started = True
            try:
                yield base, observed
            finally:
                observed.release.set()
    except OSError as exc:
        if not started and exc.errno in (errno.EPERM, errno.EACCES):
            pytest.skip(f"loopback sockets forbidden by the environment ({exc})")
        raise


def _prepare_subprocess(mirror: Mirror, base: str) -> tuple[list[str], dict[str, str]]:
    mirror.paths.ensure()
    save_config(mirror.config, mirror.paths.config_file)
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "NIMBO_CONFIG_DIR": str(mirror.paths.config_dir),
        "NIMBO_STATE_DIR": str(mirror.paths.state_dir),
        "XDG_CONFIG_HOME": str(mirror.paths.config_dir),
        "XDG_STATE_HOME": str(mirror.paths.state_dir),
    }
    return [".venv/bin/python", "tests/system/run_engine.py", base], env


def _kill_at_cut(command: list[str], env: dict[str, str], observed: _ObservedWrites) -> None:
    with subprocess.Popen(
        command, cwd=_REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as process:
        try:
            if not observed.reached.wait(_DEADLINE):
                if process.poll() is None:
                    process.kill()
                stdout, stderr = process.communicate(timeout=_DEADLINE)
                pytest.fail(
                    f"write cut was never reached: {process.returncode=} {stdout=} {stderr=}"
                )
            assert observed.writes == observed.nth_write
            assert process.poll() is None
            process.send_signal(signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=_DEADLINE)
            assert process.returncode == -signal.SIGKILL, (stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=_DEADLINE)
            observed.release.set()


def _run_subprocess(command: list[str], env: dict[str, str]) -> RunSummary:
    completed = subprocess.run(
        command, cwd=_REPO, env=env, capture_output=True, text=True, timeout=_DEADLINE
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    summary = RunSummary.from_dict(json.loads(completed.stdout))
    assert_ok(summary)
    return summary


def _assert_recovered(mirror: Mirror, command: list[str], env: dict[str, str]) -> None:
    mirror.assert_state()
    start = len(mirror.fake.calls)
    _run_subprocess(command, env)
    assert count_writes(mirror.fake, start) == 0, write_calls(mirror.fake, start)
    mirror.assert_state()


@pytest.mark.parametrize("nth_write", _CUTS, ids=lambda ordinal: f"write-{ordinal:02}")
def test_sigkill_at_seeded_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nth_write: int
) -> None:
    """Kill at one of twenty distinct writes and recover from the same on-disk state."""
    mirror = make_mirror(
        tmp_path,
        {
            **SMALL_TREE,
            "Docs/Group/Large images.bin": CHUNK_SIZE * 15 + 7,
            **{f"Docs/Group/page {i:02}.txt": str(i).encode() for i in range(12)},
        },
        seed_docs=True,
    )
    with _serve_at_cut(mirror, monkeypatch, nth_write) as (base, observed):
        command, env = _prepare_subprocess(mirror, base)
        _kill_at_cut(command, env, observed)
        assert count_writes(mirror.fake) == nth_write
        assert_no_duplicates(mirror.fake, mirror.target)
        assert_untouched(mirror.fake, mirror.protected)
        _run_subprocess(command, env)
        _assert_recovered(mirror, command, env)


def test_sigkill_mid_upload_resumes_without_resending_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill a 40 MiB upload after 22 MiB, then measure the request bodies sent on recovery."""
    total = 40 * 1024**2
    prefix = 22 * 1024**2
    mirror = make_mirror(tmp_path, {"Docs/large.bin": total}, seed_docs=True)
    # The first write opens the session, then one write per body chunk follows.
    with _serve_at_cut(mirror, monkeypatch, 1 + prefix // CHUNK_SIZE) as (base, observed):
        command, env = _prepare_subprocess(mirror, base)
        _kill_at_cut(command, env, observed)
        assert observed.chunk_bytes == prefix
        assert observed.cut_status == 308
        assert observed.cut_range == f"bytes=0-{prefix - 1}"
        before = len(observed.chunk_ranges)
        session_id = observed.chunk_ranges[-1][0]

        _run_subprocess(command, env)
        resent = observed.chunk_bytes - prefix
        assert 0 < resent < total * 0.60
        assert resent == total - prefix
        assert observed.chunk_ranges[before] == (
            session_id,
            f"bytes {prefix}-{prefix + CHUNK_SIZE - 1}/{total}",
        )
        assert {upload_id for upload_id, _ in observed.chunk_ranges[before:]} == {session_id}
        _assert_recovered(mirror, command, env)
