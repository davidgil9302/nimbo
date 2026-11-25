from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nimbo.core.errors import LockedError
from nimbo.core.lock import RunLock, is_locked


def test_second_acquire_fails(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nimbo.lock"
    with RunLock(path) as lock:
        assert path.read_text().strip() == str(os.getpid())
        for contender in (RunLock(path), lock):
            with pytest.raises(LockedError) as exc:
                contender.acquire()
            assert exc.value.code == "already_running"
        assert path.read_text().strip() == str(os.getpid())
        assert is_locked(path)


def test_lock_released_on_exit_and_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "nimbo.lock"
    with RunLock(path):
        inode = path.stat().st_ino
    assert not is_locked(path)
    with pytest.raises(ValueError, match="test failure"), RunLock(path):
        raise ValueError("test failure")
    assert not is_locked(path)
    assert path.stat().st_ino == inode
    lock = RunLock(path)
    lock.acquire()
    lock.release()
    lock.release()
    with lock:
        assert is_locked(path)


def test_lock_released_when_process_dies(tmp_path: Path) -> None:
    path = tmp_path / "nimbo.lock"
    child_code = """\
import os
import sys
from pathlib import Path
from nimbo.core.lock import RunLock

lock = RunLock(Path(sys.argv[1]))
lock.acquire()
print("ready", flush=True)
sys.stdin.readline()
os._exit(0)
"""
    with subprocess.Popen(
        [sys.executable, "-c", child_code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdin is not None and child.stdout is not None
        try:
            # The child cannot exit until its stdin closes.
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline().strip() == "ready"
            assert path.read_text().strip() == str(child.pid)
            with pytest.raises(LockedError):
                RunLock(path).acquire()
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert child.returncode == 0
    assert not is_locked(path)
    with RunLock(path):
        assert is_locked(path)


def test_is_locked(tmp_path: Path) -> None:
    path = tmp_path / "nimbo.lock"
    assert not is_locked(path)
    assert not path.exists()
    with RunLock(path):
        assert is_locked(path)
    before = path.stat()
    assert not is_locked(path)
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_ino == before.st_ino
    with RunLock(path):
        assert is_locked(path)


@pytest.mark.parametrize("existing", [False, True])
def test_lock_file_is_private(tmp_path: Path, existing: bool) -> None:
    path = tmp_path / "nimbo.lock"
    if existing:
        path.touch()
        path.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        with RunLock(path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)
