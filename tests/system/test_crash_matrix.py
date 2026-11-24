"""Cut a journaled backup at every Drive write in fresh and incremental runs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import httpx
import pytest

from nimbo.core.errors import TransportFailed
from tests.fakedrive import FakeFile, Fault, FaultPlan, SimulatedCrash
from tests.system.helpers import (
    CHUNK_SIZE,
    Mirror,
    assert_no_duplicates,
    assert_ok,
    assert_untouched,
    live_items,
    local_view,
    make_mirror,
    put_file,
    run_until_ok,
    write_calls,
)

_FRESH_TREE = {
    "Docs/Empty folder": None,
    "Docs/Album/Negatives/exposure.txt": b"negative",
    "Docs/Album/Café.txt": b"composed",
    "Docs/Album/Re\u0301sume\u0301.txt": b"decomposed",
    "Docs/emoji 📁.txt": b"emoji",
    "Docs/Work/name with space.txt": b"rename me",
    "Docs/Work/empty.txt": b"",
    "Docs/payload A.bin": CHUNK_SIZE * 6 + 1,
    "Docs/Work/2025/payload B.bin": CHUNK_SIZE * 7 + 1,
    "Docs/Work/2025/payload C.bin": CHUNK_SIZE * 8 + 1,
    **{f"Docs/Work/work-{i:02}.txt": f"part {i}".encode() for i in range(16)},
    "Docs/.DS_Store": b"ignored on the source",
}
_FRESH_WRITES = 6 + 22 + (1 + 7) + (1 + 8) + (1 + 9)
_INCREMENTAL_WRITES = 19
_INCREMENTAL_TRASH = ("Docs/Empty folder", "Docs/Work/work-00.txt")
_CrashMode = Literal["crash_before", "crash_after", "drop_response"]
_MODES: tuple[_CrashMode, ...] = ("crash_before", "crash_after", "drop_response")


def _scenario(base: Path, scenario: str) -> tuple[Mirror, dict[str, FakeFile]]:
    incremental = scenario == "incremental"
    mirror = make_mirror(base, _FRESH_TREE, seed_docs=incremental)
    if not incremental:
        return mirror, {}

    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
    live = live_items(mirror.fake, mirror.target)
    root = mirror.root
    (root / "Docs/Album").rename(root / "Docs/Work/Moved album")
    (root / "Docs/Work/name with space.txt").rename(root / "Docs/Work/2025/renamed file.txt")
    put_file(root / "Docs/Work/work-01.txt", b"EDITED")
    payload = root / "Docs/Work/2025/payload B.bin"
    put_file(payload, payload.read_bytes() + b"!")
    put_file(root / "Docs/New folder/Cafe\u0301.txt", b"new")
    put_file(root / "Docs/New folder/new payload.bin", b"+" * (CHUNK_SIZE + 1))
    (root / _INCREMENTAL_TRASH[0]).rmdir()
    (root / _INCREMENTAL_TRASH[1]).unlink()
    return mirror, {live[rel].id: live[rel] for rel in _INCREMENTAL_TRASH}


def _stop_on_retry(_: float) -> None:
    raise SimulatedCrash("Interrupted before retrying a lost response")


@pytest.mark.parametrize(
    ("scenario", "writes"), [("fresh", _FRESH_WRITES), ("incremental", _INCREMENTAL_WRITES)]
)
def test_clean_write_inventory(tmp_path: Path, scenario: str, writes: int) -> None:
    """Check that the matrix enumerates exactly as many writes as each clean run issues."""
    mirror, retired = _scenario(tmp_path, scenario)
    start = len(mirror.fake.calls)
    with mirror.open() as session:
        summary = session.engine.run(trigger="manual")
        assert_ok(summary)
        mirror.assert_state()
        calls = write_calls(mirror.fake, start)
        assert len(calls) == writes, [(call.method, call.path, call.status) for call in calls]
        if scenario == "fresh":
            assert (summary.mkdirs, summary.uploaded) == (6, 25)
        else:
            assert (
                summary.mkdirs,
                summary.moved,
                summary.uploaded,
                summary.updated,
                summary.trashed,
            ) == (1, 2, 2, 2, 2)
        for file_id, before in retired.items():
            assert mirror.fake.file(file_id) == replace(
                before, trashed=True, explicitly_trashed=True
            )
        mirror.assert_idempotent(session.engine)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("scenario", "ordinal"),
    [
        *(
            pytest.param("fresh", k, marks=pytest.mark.slow, id=f"fresh-{k:02}")
            for k in range(1, _FRESH_WRITES + 1)
        ),
        *(
            pytest.param("incremental", k, id=f"incremental-{k:02}")
            for k in range(1, _INCREMENTAL_WRITES + 1)
        ),
    ],
)
def test_recover_at_every_write(
    tmp_path: Path, scenario: str, ordinal: int, mode: _CrashMode
) -> None:
    """Crash with a durable journal at the selected request and recover on a new engine."""
    mirror, retired = _scenario(tmp_path, scenario)
    matcher = mirror.config.matcher()
    expected = local_view(mirror.root, matcher, mirror.config.active_folders)
    start = len(mirror.fake.calls)
    mirror.fake.faults.add(Fault(FaultPlan.is_write, mode, skip=ordinal - 1))
    retry_hook = {"retry_sleep": _stop_on_retry} if mode == "drop_response" else {}
    with pytest.raises(SimulatedCrash) as crash, mirror.open(**retry_hook) as session:
        session.engine.run(trigger="manual")

    calls = write_calls(mirror.fake, start)
    assert len(calls) == ordinal
    assert (calls[-1].status is None) == (mode == "crash_before")
    if mode == "drop_response":
        retry_error = crash.value.__context__
        assert isinstance(retry_error, TransportFailed)
        assert isinstance(retry_error.__cause__, httpx.ReadTimeout)
    assert_no_duplicates(mirror.fake, mirror.target)
    assert_untouched(mirror.fake, mirror.protected)

    with mirror.open() as recovered:
        run_until_ok(recovered.engine)
        assert recovered.index.integrity_ok()
        mirror.assert_state()
        assert local_view(mirror.root, matcher, mirror.config.active_folders) == expected
        for file_id, before in retired.items():
            assert mirror.fake.file(file_id) == replace(
                before, trashed=True, explicitly_trashed=True
            )
        mirror.assert_idempotent(recovered.engine)
    assert all(call.method != "DELETE" for call in write_calls(mirror.fake, start))


@pytest.mark.parametrize("ordinal", (1, 7, _FRESH_WRITES))
def test_retry_lost_response_without_restarting(tmp_path: Path, ordinal: int) -> None:
    """Let normal retry logic handle a lost mkdir, multipart or final chunk response."""
    mirror, _ = _scenario(tmp_path, "fresh")
    mirror.fake.faults.add(Fault(FaultPlan.is_write, "drop_response", skip=ordinal - 1))
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
        mirror.assert_idempotent(session.engine)
