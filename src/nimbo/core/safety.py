"""Local and remote preflight checks and single-use deletion approvals."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nimbo.core.config import Config, SafetyConfig
from nimbo.core.errors import SafetyError
from nimbo.core.fsutil import read_json, write_json
from nimbo.core.model import SENTINEL_NAME, About, BrakeDecision, OpKind, Plan, utc_now_iso


@dataclass(frozen=True, slots=True)
class Problem:
    """A preflight failure code and its optional diagnostic detail."""

    code: str
    detail: str = ""


@contextmanager
def _lock_directory(path: Path) -> Iterator[None]:
    # The directory inode survives atomic replacement of its files.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_object(path: Path) -> dict[str, object] | None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        data = read_json(path)
    except (OSError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def read_sentinel(root: Path) -> str | None:
    """Return the Nimbo root UUID, or None for a missing, invalid or foreign marker."""
    data = _read_object(root / SENTINEL_NAME)
    if data is None or data.get("app") != "nimbo":
        return None
    root_uuid = data.get("uuid")
    return root_uuid if isinstance(root_uuid, str) and root_uuid else None


def write_sentinel(root: Path, root_uuid: str) -> None:
    """Atomically initialize a root marker or leave a matching marker in place.

    Raise SafetyError for a conflicting or unrecognized existing marker.
    Raise ValueError for an empty UUID, and propagate filesystem errors.
    """
    if not root_uuid:
        raise ValueError("root_uuid")
    path = root / SENTINEL_NAME
    with _lock_directory(root):
        try:
            path.lstat()
        except FileNotFoundError:
            write_json(path, {"app": "nimbo", "uuid": root_uuid, "created": utc_now_iso()})
        else:
            if read_sentinel(root) != root_uuid:
                raise SafetyError(code="sentinel_mismatch")


def _folder_problem(root: Path, name: str) -> Problem | None:
    path = root / name
    try:
        # Match the scanner's treatment of configured symlinks and non-directories.
        if not stat.S_ISDIR(path.lstat().st_mode):
            return Problem("folder_missing", name)
        with os.scandir(path) as entries:
            if next(entries, None) is None:
                return Problem("folder_empty", name)
    except (FileNotFoundError, NotADirectoryError):
        return Problem("folder_missing", name)
    except OSError:
        return Problem("folder_unreadable", name)
    return None


def check_source(cfg: Config, expected_uuid: str | None) -> list[Problem]:
    """Return all mount, root, marker and active-folder problems in that order.

    A None expected UUID accepts any valid Nimbo marker. Active folders are
    inspected in configuration order and any directory entry counts as content.
    """
    root = cfg.source.root
    problems: list[Problem] = []
    if cfg.source.require_mount:
        try:
            mounted = os.path.ismount(root)
        except OSError:
            mounted = False
        if not mounted:
            problems.append(Problem("source_not_mounted"))

    try:
        is_directory = root.is_dir()
    except OSError:
        is_directory = False
    if not is_directory:
        problems.append(Problem("source_missing"))

    root_uuid = read_sentinel(root)
    if root_uuid is None:
        problems.append(Problem("sentinel_missing"))
    elif expected_uuid is not None and root_uuid != expected_uuid:
        problems.append(Problem("sentinel_mismatch"))

    for name in cfg.active_folders:
        problem = _folder_problem(root, name)
        if problem is not None:
            problems.append(problem)
    return problems


def network_available(
    host: str = "www.googleapis.com", port: int = 443, timeout: float = 5.0
) -> bool:
    """Return whether a TCP connection succeeds using the given socket timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def evaluate_brakes(plan: Plan, cfg: SafetyConfig) -> BrakeDecision:
    """Check deletion statistics against the limits and bind the result to the plan."""
    stats = plan.stats
    reasons = [
        f"folder_ratio:{name}"
        for name, count in stats.per_folder_trashed.items()
        if count >= cfg.ratio_min_files
        and count / max(1, stats.per_folder_remote_files.get(name, 0)) > cfg.max_delete_ratio
    ]
    if stats.trashed_files > cfg.max_deletes:
        reasons.append("too_many_deletes")
    fingerprint = plan.fingerprint()
    digests = sorted(
        op.trash_ids_digest
        for op in plan.ops
        if op.kind is OpKind.TRASH and op.trash_ids_digest is not None
    )
    if digests:
        payload = json.dumps([fingerprint, digests], separators=(",", ":"))
        fingerprint = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return BrakeDecision(bool(reasons), tuple(sorted(reasons)), fingerprint)


def check_space(plan: Plan, about: About) -> Problem | None:
    """Return a quota problem if the planned upload and update bytes exceed free space."""
    free = about.free
    if free is not None and plan.stats.bytes_to_send > free:
        return Problem("storage_insufficient")
    return None


def write_approval(
    path: Path,
    fingerprint: str,
    *,
    now: datetime,
    ttl: timedelta = timedelta(hours=24),
) -> None:
    """Atomically replace the approval with a fingerprint and expiry, propagating I/O errors."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_directory(path.parent):
        write_json(path, {"fingerprint": fingerprint, "expires": (now + ttl).isoformat()})


def consume_approval(path: Path, fingerprint: str, *, now: datetime) -> bool:
    """Remove an approval and accept it only if its fingerprint matches and expiry is future.

    Return False when the approval is absent, invalid or could not be removed.
    """
    try:
        with _lock_directory(path.parent):
            data = _read_object(path)
            path.unlink()
    except OSError:
        return False

    if data is None or data.get("fingerprint") != fingerprint:
        return False
    expires = data.get("expires")
    if not isinstance(expires, str):
        return False
    try:
        return now < datetime.fromisoformat(expires)
    except (TypeError, ValueError):
        return False
