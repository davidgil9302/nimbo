"""System-test fixtures and independent local/Drive mirror assertions."""

from __future__ import annotations

import hashlib
import os
import shutil
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from random import Random

import httpx

from nimbo.core.config import (
    Config,
    OAuthConfig,
    SafetyConfig,
    SourceConfig,
    TargetConfig,
    TransferConfig,
)
from nimbo.core.drive import DriveClient
from nimbo.core.engine import Engine, EngineDeps
from nimbo.core.filters import Matcher
from nimbo.core.index import Index
from nimbo.core.model import RunResult, RunSummary
from nimbo.core.paths import Paths
from nimbo.core.ratelimit import RateLimiter, RetryPolicy
from nimbo.core.safety import write_sentinel
from nimbo.core.transfer import Downloader, Uploader
from tests.fakedrive import CallRecord, FakeDrive, FakeFile, FaultPlan

CHUNK_SIZE = 262144
MULTIPART_MAX = 64 * 1024
TOKEN = "static-system-token"
FIXED_TIME = datetime(2025, 1, 1, tzinfo=UTC)
FOLDERS = ("Docs",)
ANCHOR = "Docs/anchor.txt"
_LOCAL_MTIME_NS = 1_710_000_000_000_000_000
_NAMES = ("Café", "Re\u0301sume\u0301", "with space", "📁")

TreeSpec = Mapping[str, bytes | int | None]
View = dict[str, tuple[bool, int, str | None]]


def put_file(path: Path, content: bytes) -> None:
    """Write fixture bytes with an observable mtime, even for same-size edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.stat().st_mtime_ns if path.exists() else _LOCAL_MTIME_NS
    path.write_bytes(content)
    stamp = before + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def build_tree(root: Path, spec: TreeSpec) -> None:
    """Create a tree from relative paths mapped to bytes, byte counts or None for folders."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, value in spec.items():
        path = root / rel
        if value is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            content = (
                hashlib.shake_256(rel.encode()).digest(value) if isinstance(value, int) else value
            )
            put_file(path, content)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def local_view(source_root: Path, matcher: Matcher, folders: tuple[str, ...]) -> View:
    """Read the filtered fixture paths and hash their bytes independently of the engine."""
    result: View = {}
    for folder in folders:
        for parent, dirs, files in os.walk(source_root / folder):
            parent_path = Path(parent)
            rel = _nfc(parent_path.relative_to(source_root).as_posix())
            if matcher.excluded(rel, True):
                dirs.clear()
                continue
            assert rel not in result, f"colliding local paths at {rel!r}"
            result[rel] = (True, 0, None)
            for name in files:
                file_rel = f"{rel}/{_nfc(name)}"
                if matcher.excluded(file_rel, False):
                    continue
                assert file_rel not in result, f"colliding local paths at {file_rel!r}"
                with (parent_path / name).open("rb") as source:
                    digest = hashlib.file_digest(source, lambda: hashlib.md5(usedforsecurity=False))
                    result[file_rel] = (
                        False,
                        os.fstat(source.fileno()).st_size,
                        digest.hexdigest(),
                    )
    return result


def assert_mirror(
    fake: FakeDrive, root_id: str, source_root: Path, matcher: Matcher, folders: tuple[str, ...]
) -> None:
    """Compare all visible, in-scope binary files and directories with the local view."""
    actual = {
        rel: info
        for rel, info in fake.snapshot(root_id).items()
        if rel.split("/", 1)[0] in folders
        and (info[0] or info[2] is not None)
        and not matcher.excluded(_nfc(rel), info[0])
    }
    assert actual == local_view(source_root, matcher, folders)


def live_items(fake: FakeDrive, root_id: str) -> dict[str, FakeFile]:
    """Walk visible records by physical parent and reject duplicate sibling names in NFC."""
    names: dict[str, list[str]] = defaultdict(list)
    for rel in fake.snapshot(root_id):
        parent, _, name = rel.rpartition("/")
        names[parent].append(name)
    items: dict[str, FakeFile] = {}
    pending = [("", root_id)]
    while pending:
        parent, parent_id = pending.pop()
        siblings: dict[str, str] = {}
        for name in names[parent]:
            rel = f"{parent}/{name}" if parent else name
            matches = [item for item in fake.find(parent_id, name) if not item.trashed]
            assert len(matches) == 1, (rel, [item.id for item in matches])
            item = matches[0]
            canonical = _nfc(name)
            assert canonical not in siblings, (rel, siblings.get(canonical), item.id)
            siblings[canonical] = item.id
            items[rel] = item
            if item.is_folder:
                pending.append((rel, item.id))
    return items


def assert_no_duplicates(fake: FakeDrive, root_id: str) -> None:
    """Reject duplicate visible sibling names, including NFC-equivalent spellings."""
    live_items(fake, root_id)


def assert_untouched(fake: FakeDrive, ids_snapshot: Mapping[str, FakeFile]) -> None:
    """Require the protected IDs, metadata, bytes and effective trash state to survive."""
    for file_id, expected in ids_snapshot.items():
        assert fake.file(file_id) == expected, expected.name


def _is_write(call: CallRecord) -> bool:
    return FaultPlan.is_write(httpx.Request(call.method, "https://example.test" + call.path))


def write_calls(fake: FakeDrive, start: int = 0) -> list[CallRecord]:
    """Return recorded mutation requests since a cursor, including failed attempts."""
    return [call for call in fake.calls[start:] if _is_write(call)]


def count_writes(fake: FakeDrive, start: int = 0) -> int:
    """Count mutation requests since a cursor using the fake's fault predicate."""
    return len(write_calls(fake, start))


def _no_sleep(_: float) -> None:
    pass


def _no_limiter_wait(delay: float) -> None:
    raise AssertionError(f"Test limiter unexpectedly requested {delay} seconds of sleep")


class _StaticToken:
    def token(self) -> str:
        return TOKEN

    def invalidate(self) -> None:
        pass


class _Notifier:
    def run_finished(self, summary: RunSummary, *, manual: bool) -> None:
        pass


@dataclass
class EngineSession:
    """Expose the engine, its SQLite journal and its Drive client to a test."""

    engine: Engine
    index: Index
    drive: DriveClient


@contextmanager
def open_engine(
    config: Config,
    paths: Paths,
    client: httpx.Client,
    *,
    retry_sleep: Callable[[float], None] = _no_sleep,
) -> Iterator[EngineSession]:
    """Build an engine over an injected API, closing the index without a commit on exit."""
    paths.ensure()
    drive = DriveClient(
        client,
        _StaticToken(),
        RateLimiter(
            reads_per_second=10_000,
            writes_per_second=10_000,
            clock=lambda: 0.0,
            sleep=_no_limiter_wait,
        ),
        RetryPolicy(max_attempts=8, base_delay=0),
        api_base=str(client.base_url),
        sleep=retry_sleep,
    )
    index = Index(paths.db_file)
    try:
        deps = EngineDeps(
            config,
            paths,
            index,
            drive,
            Uploader(drive, index, chunk_size=CHUNK_SIZE, multipart_max=MULTIPART_MAX),
            Downloader(drive),
            _Notifier(),
            clock=lambda: FIXED_TIME,
            network_check=lambda: True,
        )
        yield EngineSession(Engine(deps), index, drive)
    finally:
        index.close()


def engine_config(root: Path, target: str, *, parallel: int = 1) -> Config:
    """Return an isolated configuration with approved fixture deletions."""
    return Config(
        SourceConfig(root, FOLDERS, require_mount=False),
        TargetConfig(target),
        OAuthConfig("system-test.apps.googleusercontent.com", "system-test-secret"),
        safety=SafetyConfig(max_deletes=10_000, max_delete_ratio=1.0),
        transfer=TransferConfig(parallel_uploads=parallel),
    )


@dataclass
class Mirror:
    """Hold the source, remote target, protected records and persistent journal location."""

    root: Path
    fake: FakeDrive
    config: Config
    paths: Paths
    outside: str
    protected: dict[str, FakeFile] = field(default_factory=dict)

    @property
    def target(self) -> str:
        """Return the target folder's fake Drive ID."""
        return self.config.target.folder_id

    def protect(self, *ids: str) -> None:
        """Capture exactly the selected IDs at the time they become protected."""
        self.protected.update((file_id, self.fake.file(file_id)) for file_id in ids)

    @contextmanager
    def open(self, *, retry_sleep: Callable[[float], None] = _no_sleep) -> Iterator[EngineSession]:
        """Open a new HTTP client and engine using the same on-disk journal."""
        with (
            self.fake.client(trust_env=False) as client,
            open_engine(self.config, self.paths, client, retry_sleep=retry_sleep) as session,
        ):
            yield session

    def assert_state(self) -> None:
        """Check mirror equality, uniqueness and all protected records."""
        assert_no_duplicates(self.fake, self.target)
        assert_untouched(self.fake, self.protected)
        assert_mirror(
            self.fake, self.target, self.root, self.config.matcher(), self.config.active_folders
        )

    def assert_idempotent(self, engine: Engine) -> None:
        """Require a second successful run with zero write requests and the same mirror."""
        start = len(self.fake.calls)
        assert_ok(engine.run(trigger="manual"))
        assert count_writes(self.fake, start) == 0, write_calls(self.fake, start)
        self.assert_state()


def make_mirror(
    base: Path, spec: TreeSpec, *, seed_docs: bool = False, parallel: int = 1
) -> Mirror:
    """Create local fixtures, a fresh fake Drive and examples of untouchable records."""
    root = base / "source"
    build_tree(root, spec)
    write_sentinel(root, "system-test-root")
    fake = FakeDrive(clock=FIXED_TIME.timestamp)
    fake.oauth.add_access_token(TOKEN)
    target = fake.add_folder("Backup")
    outside = fake.add_folder("Outside")
    mirror = Mirror(
        root,
        fake,
        engine_config(root, target, parallel=parallel),
        Paths(base / "config", base / "state"),
        outside,
    )
    excluded = fake.add_folder("node_modules", target)
    mirror.protect(
        outside,
        fake.add_file("outside.txt", outside, b"outside target"),
        fake.add_folder("Outside sibling", fake.root_id),
        fake.add_native("Root cloud note", target),
        excluded,
        fake.add_file("ignored.txt", excluded, b"ignored sibling"),
    )
    if seed_docs:
        docs = fake.add_folder("Docs", target)
        excluded = fake.add_folder("node_modules", docs)
        mirror.protect(
            fake.add_native("Cloud notes", docs),
            fake.add_file(".DS_Store", docs, b"remote exclusion"),
            excluded,
            fake.add_file("nested.txt", excluded, b"excluded by ancestor"),
        )
    return mirror


def assert_ok(summary: RunSummary) -> None:
    """Require a successful normal run with no reported op failures."""
    assert summary.result is RunResult.OK and not summary.failed, summary


def run_until_ok(engine: Engine, *, max_runs: int = 3) -> RunSummary:
    """Run up to the limit, accepting only file-in-use partials before success."""
    for _ in range(max_runs):
        summary = engine.run(trigger="manual")
        if summary.result is RunResult.OK:
            assert_ok(summary)
            return summary
        assert summary.result is RunResult.PARTIAL and summary.reason != "internal_error", summary
        assert summary.failed and {code for _, code in summary.failed} == {"file_in_use"}, summary
    raise AssertionError(f"Engine did not converge within {max_runs} runs: {summary!r}")


LOCAL_ACTIONS = (
    "create_file",
    "edit_same_size",
    "edit_size",
    "rename_file",
    "move_file",
    "delete_file",
    "create_folder",
    "rename_folder",
    "move_folder",
    "delete_folder",
)
REMOTE_ACTIONS = ("edit", "native", "extra", "move_out")
SMALL_TREE: TreeSpec = {
    ANCHOR: b"stable anchor",
    "Docs/Group/Cafe\u0301.txt": b"accented",
    "Docs/Group/empty 📁.txt": b"",
    "Docs/.DS_Store": b"local exclusion",
}


def mutate_local(root: Path, action: str, rng: Random, serial: int) -> bool:
    """Apply one generated filesystem change and report whether a candidate existed."""
    docs = root / "Docs"
    paths = sorted(path for path in docs.rglob("*") if path.name != ".DS_Store")
    directories = [path for path in paths if path.is_dir()]
    files = [path for path in paths if path.is_file() and path != root / ANCHOR]
    parents = [docs, *directories]
    name = f"{rng.choice(_NAMES)} {serial}"
    if action == "create_file":
        size = rng.choice((0, 4, 19, CHUNK_SIZE + 17))
        put_file(rng.choice(parents) / f"{name}.txt", rng.randbytes(size))
        return True
    if action == "create_folder":
        (rng.choice(parents) / name).mkdir()
        return True
    if action == "edit_same_size":
        files = [path for path in files if path.stat().st_size > 0]
    candidates = files if action.endswith("_file") or action.startswith("edit_") else directories
    if not candidates:
        return False

    path = rng.choice(candidates)
    match action:
        case "edit_same_size":
            data = path.read_bytes()
            put_file(path, bytes([data[0] ^ 1]) + data[1:])
        case "edit_size":
            put_file(path, path.read_bytes() + rng.randbytes(rng.randint(1, 19)))
        case "rename_file" | "rename_folder":
            path.rename(path.with_name(name + path.suffix))
        case "move_file" | "move_folder":
            destinations = [p for p in parents if p != path.parent and not p.is_relative_to(path)]
            if not destinations:
                return False
            path.rename(rng.choice(destinations) / (name + path.suffix))
        case "delete_file":
            path.unlink()
        case "delete_folder":
            shutil.rmtree(path)
        case _:
            raise ValueError(action)
    return True


def remote_noise(mirror: Mirror, drive: DriveClient, action: str, rng: Random, serial: int) -> bool:
    """Inject one external Drive mutation and capture newly protected records."""
    fake = mirror.fake
    docs = next(item for item in fake.find(mirror.target, "Docs") if not item.trashed)
    match action:
        case "native":
            mirror.protect(fake.add_native(f"Cloud note {serial}", docs.id))
        case "extra":
            fake.add_file(f"Extra 📎 {serial}.bin", docs.id, rng.randbytes(21))
        case "edit":
            candidates = [item for item in fake.find(docs.id, "anchor.txt") if not item.trashed]
            if not candidates:
                return False
            anchor = candidates[0]
            data = rng.randbytes(rng.choice((len(anchor.content), len(anchor.content) + 1)))
            drive.multipart_upload(
                data=data,
                file_id=anchor.id,
                name=None,
                parent_id=None,
                modified_ms=anchor.modified_ms + 1000,
                properties=None,
                update=True,
            )
        case "move_out":
            candidates = [
                item
                for item in live_items(fake, mirror.target).values()
                if item.parent_id == docs.id and item.id not in mirror.protected
            ]
            if not candidates:
                return False
            item = rng.choice(candidates)
            descendants = live_items(fake, item.id).values() if item.is_folder else ()
            drive.update_metadata(item.id, add_parent=mirror.outside, remove_parents=[docs.id])
            mirror.protect(item.id, *(child.id for child in descendants))
        case _:
            raise ValueError(action)
    return True
