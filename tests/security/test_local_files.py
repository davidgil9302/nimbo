"""Regression tests for the mirror's local file boundary."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nimbo.core.model import PART_SUFFIX, RunResult
from nimbo.core.scanner import scan
from tests.system.helpers import ANCHOR, assert_ok, make_mirror, open_engine


@pytest.mark.parametrize("private", ["Private", "Docs/private"], ids=["unselected", "nobackup"])
def test_upload_rejects_directory_swap(tmp_path: Path, private: str) -> None:
    rel = "Docs/shared/sub/a.txt"
    tree = {ANCHOR: b"anchor", rel: b"public", f"{private}/a.txt": b"secret"}
    if private.startswith("Docs/"):
        tree[f"{private}/.nobackup"] = b""
    mirror = make_mirror(tmp_path, tree)
    branch = (mirror.root / rel).parent
    target = mirror.root / private
    assert (branch / "a.txt").stat().st_mtime_ns == (target / "a.txt").stat().st_mtime_ns

    def swap_after_scan(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/generateIds") and not branch.is_symlink():
            branch.rename(tmp_path / "original")
            branch.symlink_to(target, target_is_directory=True)

    with (
        mirror.fake.client(event_hooks={"response": [swap_after_scan]}) as client,
        open_engine(mirror.config, mirror.paths, client) as session,
    ):
        summary = session.engine.run(trigger="manual")

    assert branch.is_symlink()
    assert rel not in mirror.fake.snapshot(mirror.target)
    assert summary.result is RunResult.PARTIAL
    assert summary.failed == [(rel, "file_in_use")]


@pytest.mark.parametrize("symlink", [True, False], ids=["symlink", "different-inode"])
def test_scanner_skips_directory_replaced_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    mirror = make_mirror(
        tmp_path, {"Docs/shared/sub/public.txt": b"public", "Private/secret.txt": b"secret"}
    )
    parent = mirror.root / "Docs/shared"
    branch = parent / "sub"
    target = mirror.root / "Private"
    original_scandir = os.scandir
    swapped = False

    def swap_after_stat(child: os.DirEntry[str], *, follow_symlinks: bool) -> os.stat_result:
        nonlocal swapped
        info = child.stat(follow_symlinks=follow_symlinks)
        branch.rename(tmp_path / "original")
        if symlink:
            branch.symlink_to(target, target_is_directory=True)
        else:
            target.rename(branch)
        swapped = True
        return info

    @contextmanager
    def scandir(path: Path | int) -> Iterator[Iterator[os.DirEntry[str] | SimpleNamespace]]:
        listed = Path(os.readlink(f"/proc/self/fd/{path}")) if isinstance(path, int) else path
        with original_scandir(path) as children:
            yield (
                SimpleNamespace(
                    name=child.name, path=child.path, stat=partial(swap_after_stat, child)
                )
                if listed == parent and child.name == branch.name
                else child
                for child in children
            )

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        result = scan(mirror.root, mirror.config.active_folders, mirror.config.matcher())

    assert swapped
    assert set(result.entries) == {"Docs", "Docs/shared", "Docs/shared/sub"}
    assert result.protected == ["Docs/shared/sub"]
    assert [(problem.code, problem.rel) for problem in result.problems] == [
        ("unreadable", "Docs/shared/sub")
    ]


@pytest.mark.parametrize("mask", [0o022, 0o777], ids=["umask-022", "umask-777"])
def test_undo_restores_owner_only_mode(tmp_path: Path, mask: int) -> None:
    rel = "Docs/a.txt"
    mirror = make_mirror(tmp_path, {ANCHOR: b"anchor", rel: b"restore"})
    dest = mirror.root / rel
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        dest.unlink()
        run = session.engine.run(trigger="manual")
        assert_ok(run)
        assert run.trashed == 1

        original_mask = os.umask(mask)
        try:
            restored = session.engine.undo(run.run_id)
        finally:
            os.umask(original_mask)

    assert restored.restored_files == 1 and restored.failed == []
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.read_bytes() == b"restore"


@pytest.mark.parametrize("symlink", [False, True], ids=["regular-file", "symlink"])
def test_undo_ignores_existing_legacy_staging_name(tmp_path: Path, symlink: bool) -> None:
    rel = "Docs/a.txt"
    mirror = make_mirror(tmp_path, {ANCHOR: b"anchor", rel: b"restore", "Private/a.txt": b"keep"})
    dest = mirror.root / rel
    part = dest.with_name(dest.name + PART_SUFFIX)
    private = mirror.root / "Private/a.txt"
    with mirror.open() as session:
        assert_ok(session.engine.run(trigger="manual"))
        dest.unlink()
        run = session.engine.run(trigger="manual")
        assert_ok(run)
        assert run.trashed == 1

        if symlink:
            part.symlink_to(private)
        else:
            part.write_bytes(b"leftover")
        before = private.stat()
        restored = session.engine.undo(run.run_id)

    assert restored.restored_files == 1 and restored.failed == []
    assert dest.is_file() and not dest.is_symlink() and dest.read_bytes() == b"restore"
    assert part.is_symlink() == symlink
    if symlink:
        assert part.readlink() == private
    else:
        assert part.read_bytes() == b"leftover"
    assert private.stat().st_mtime_ns == before.st_mtime_ns
    assert private.read_bytes() == b"keep"
    assert list(dest.parent.glob(f"*{PART_SUFFIX}")) == [part]
