"""Run the seeded soak with NIMBO_SOAK=100 for the longer local check."""

from __future__ import annotations

import os
from pathlib import Path
from random import Random

import httpx
import pytest

from tests.fakedrive import Fault
from tests.system.helpers import (
    CHUNK_SIZE,
    LOCAL_ACTIONS,
    REMOTE_ACTIONS,
    SMALL_TREE,
    assert_ok,
    make_mirror,
    mutate_local,
    put_file,
    remote_noise,
    run_until_ok,
)

pytestmark = pytest.mark.slow
_SEED = 902391


def _faults(rng: Random) -> list[Fault]:
    def selected(_: httpx.Request) -> bool:
        return rng.randrange(80) == 0

    # Four independent chances of 1/80 give a combined rate near 5%.
    return [
        Fault(selected, "status", status=503, times=100_000),
        Fault(selected, "status", status=429, reason="rateLimitExceeded", times=100_000),
        Fault(selected, "transport_error", times=100_000),
        Fault(selected, "drop_response", times=100_000),
    ]


def test_engine_soak(tmp_path: Path) -> None:
    """Back up many generations of filesystem and Drive changes under transient faults."""
    rounds = int(os.environ.get("NIMBO_SOAK", "20"))
    assert rounds > 0, "NIMBO_SOAK must be positive"
    rng = Random(_SEED)
    fault_rng = Random(_SEED + 1)
    mirror = make_mirror(tmp_path, SMALL_TREE, seed_docs=True, parallel=4)
    growing = mirror.root / "Docs/growing 📦.bin"
    initial_size = CHUNK_SIZE * 2 + 1
    growth_round = rng.randrange(min(rounds, 10))
    changed_while_uploading = False

    def grow_during_first_chunk(request: httpx.Request) -> bool:
        nonlocal changed_while_uploading
        if (
            not changed_while_uploading
            and request.method == "PUT"
            and request.headers.get("Content-Range") == f"bytes 0-{CHUNK_SIZE - 1}/{initial_size}"
        ):
            put_file(growing, growing.read_bytes() + b"grew during upload")
            changed_while_uploading = True
        return False

    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        mirror.assert_state()
        mirror.assert_idempotent(session.engine)
        serial = 0
        actions = list(LOCAL_ACTIONS)
        for iteration in range(rounds):
            chosen = []
            for _ in range(3):
                if not actions:
                    actions = list(LOCAL_ACTIONS)
                rng.shuffle(actions)
                action = actions.pop()
                changed = mutate_local(mirror.root, action, rng, serial)
                chosen.append((action, changed))
                serial += 1
            external = rng.choice(REMOTE_ACTIONS)
            remote_noise(mirror, session.drive, external, rng, serial)
            serial += 1

            if iteration == growth_round:
                put_file(growing, b"x" * initial_size)
                mirror.fake.faults.add(Fault(grow_during_first_chunk, "delay"))
            for fault in _faults(fault_rng):
                mirror.fake.faults.add(fault)
            try:
                run_until_ok(session.engine, max_runs=2)
            except AssertionError as exc:
                exc.add_note(f"{_SEED=}, {iteration=}, {chosen=}, {external=}")
                raise
            finally:
                mirror.fake.faults.clear()

            try:
                mirror.assert_state()
                mirror.assert_idempotent(session.engine)
            except AssertionError as exc:
                exc.add_note(f"{_SEED=}, {iteration=}, {chosen=}, {external=}")
                raise
    assert changed_while_uploading
