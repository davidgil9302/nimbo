"""Isolated Drive targets for the opt-in tests."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest

from nimbo.app import context
from nimbo.core.config import (
    Config,
    SafetyConfig,
    SourceConfig,
    TargetConfig,
    TransferConfig,
    load_config,
    parse_folder_id,
)
from nimbo.core.drive import DriveClient
from nimbo.core.engine import Engine
from nimbo.core.errors import ConfigError
from nimbo.core.model import RemoteFile, RunResult, RunSummary
from nimbo.core.paths import Paths
from nimbo.core.safety import write_sentinel

DOC_MIME = "application/vnd.google-apps.document"
FOLDERS = ("Projects",)


def _folder_id(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        pytest.skip(f"Set {variable} to opt in to these Drive tests")
    try:
        return parse_folder_id(value)
    except ConfigError:
        pytest.fail(f"{variable} must identify a Drive folder", pytrace=False)


@dataclass(repr=False)
class _Sandbox:
    drive: DriveClient
    config: Config
    folder_id: str

    def check_parent(self, parent_id: str) -> None:
        """Require a live folder in the sandbox and outside the configured target."""
        protected = self.config.target.folder_id
        seen = set()
        current = parent_id
        while True:
            if current == protected:
                pytest.fail(
                    "The Drive sandbox is inside or equal to the backup target", pytrace=False
                )
            if current in seen:
                pytest.fail("The Drive sandbox has cyclic ancestry", pytrace=False)
            seen.add(current)
            folder = self.drive.get(current)
            if folder is None or folder.trashed or not folder.is_folder or folder.id != current:
                pytest.fail("The Drive sandbox ancestry could not be verified", pytrace=False)
            if not folder.parents:
                break
            if len(folder.parents) != 1:
                pytest.fail("The Drive sandbox has ambiguous ancestry", pytrace=False)
            (current,) = folder.parents
        if self.folder_id not in seen:
            pytest.fail("The selected parent is outside NIMBO_E2E_FOLDER_ID", pytrace=False)


@pytest.fixture(scope="session")
def drive_sandbox() -> Iterator[_Sandbox]:
    """Load saved settings before per-test XDG isolation and verify the sandbox."""
    folder_id = _folder_id("NIMBO_E2E_FOLDER_ID")
    try:
        config = load_config(Paths.default().config_file)
    except ConfigError:
        pytest.fail("Save a valid backup configuration before running Drive tests", pytrace=False)
    if folder_id == config.target.folder_id:
        pytest.fail("NIMBO_E2E_FOLDER_ID is the configured backup target", pytrace=False)

    hooks = context.TestHooks()
    with context.new_http_client() as http:
        tokens = context.token_provider(config.oauth, context.secret_store(hooks), http, hooks)
        drive = context.make_drive(http, tokens, TransferConfig(), hooks)
        sandbox = _Sandbox(drive, config, folder_id)
        sandbox.check_parent(folder_id)
        yield sandbox


@dataclass
class DriveTraffic:
    """Observe Drive mutation requests and resumable offsets without retaining URLs or tokens."""

    stop: Event | None = field(default=None, repr=False)
    writes: int = 0
    starts: int = 0
    probes: int = 0
    offsets: list[int] = field(default_factory=list)
    confirmed: int = 0

    def request(self, request: httpx.Request) -> None:
        """Count mutation attempts and record the offsets of resumable chunk requests."""
        if not request.url.path.startswith(("/drive/v3/", "/upload/drive/v3/")):
            return
        if request.method in ("POST", "PATCH", "DELETE"):
            self.writes += 1
            self.starts += request.url.params.get("uploadType") == "resumable"
        elif request.method == "PUT":
            byte_range = request.headers.get("Content-Range", "")
            if byte_range.startswith("bytes */"):
                self.probes += 1
            else:
                self.writes += 1
                if match := re.fullmatch(r"bytes (\d+)-\d+/\d+", byte_range):
                    self.offsets.append(int(match[1]))

    def response(self, response: httpx.Response) -> None:
        """Request a stop immediately after Drive confirms the first uploaded chunk."""
        if self.stop is None or response.status_code != 308 or response.request.method != "PUT":
            return
        if match := re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", "")):
            self.confirmed = int(match[1]) + 1
            self.stop.set()


class _QuietNotifier:
    def run_finished(self, summary: RunSummary, *, manual: bool) -> None:
        """Suppress desktop notifications from test runs."""


@dataclass(repr=False)
class DriveCase:
    """A single test's remote folder and temporary source and engine state."""

    sandbox: _Sandbox
    target_id: str
    parent_id: str
    name: str
    paths: Paths
    config: Config

    @property
    def drive(self) -> DriveClient:
        """Return the separate API client used for assertions and external edits."""
        return self.sandbox.drive

    def _check_root(self, item: RemoteFile | None, *, allow_trashed: bool = False) -> None:
        self.sandbox.check_parent(self.parent_id)
        if (
            item is None
            or item.id != self.target_id
            or item.parents != (self.parent_id,)
            or item.name != self.name
            or not item.is_folder
            or (item.trashed and not allow_trashed)
        ):
            pytest.fail(
                "The test folder is no longer at its expected Drive location", pytrace=False
            )

    def _check_item(self, item_id: str, *, folder: bool = False) -> None:
        self._check_root(self.drive.get(self.target_id))
        if folder:
            if self.drive.live_folder_in_subtree(item_id, self.target_id):
                return
        else:
            item = self.drive.get(item_id)
            if (
                item is not None
                and item.id == item_id
                and not item.trashed
                and not item.is_folder
                and not item.is_google_native
                and len(item.parents) == 1
                and self.drive.live_folder_in_subtree(item.parents[0], self.target_id)
            ):
                return
        pytest.fail("Refusing to write outside the test folder", pytrace=False)

    @contextmanager
    def engine(self, traffic: DriveTraffic | None = None) -> Iterator[Engine]:
        """Open the real engine against this test's folder, with optional HTTP observation."""
        self._check_root(self.drive.get(self.target_id))
        new_http_client = context.new_http_client

        def observe_client(*, loopback: bool = False) -> httpx.Client:
            client = new_http_client(loopback=loopback)
            if traffic is not None:
                client.event_hooks["request"].append(traffic.request)
                client.event_hooks["response"].append(traffic.response)
            return client

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(context, "new_http_client", observe_client)
            engine, close = context.build_engine(
                self.paths, self.config, env={}, notifier=_QuietNotifier()
            )
        try:
            yield engine
        finally:
            close()

    def run(
        self, *, expected: RunResult = RunResult.OK, traffic: DriveTraffic | None = None
    ) -> RunSummary:
        """Run a backup with a fresh CLI-style engine and assert its result."""
        with self.engine(traffic) as engine:
            summary = engine.run(
                trigger="manual", stop=traffic.stop if traffic is not None else None
            )
        assert summary.result == expected, (summary.result, summary.reason, summary.failed)
        return summary

    def write_binary(
        self,
        item: RemoteFile,
        *,
        data: bytes,
        properties: Mapping[str, str],
        duplicate: bool = False,
    ) -> RemoteFile:
        """Replace bytes at an in-scope ID or create an in-scope sibling with the same name."""
        self._check_item(item.id)
        assert item.modified_ms is not None and len(item.parents) == 1
        (parent_id,) = item.parents
        file_id = self.drive.generate_ids(1)[0] if duplicate else item.id

        return self.drive._multipart(
            data=data,
            file_id=file_id,
            name=item.name if duplicate else None,
            parent_id=parent_id if duplicate else None,
            modified_ms=item.modified_ms,
            properties=properties,
            update=not duplicate,
            before_send=lambda: self._check_item(
                parent_id if duplicate else item.id, folder=duplicate
            ),
        )

    def create_doc(self, parent_id: str) -> RemoteFile:
        """Create an empty Google Doc under a verified in-scope parent."""
        return self.drive._metadata_write(
            "POST",
            "/drive/v3/files",
            {"name": "native document", "mimeType": DOC_MIME, "parents": [parent_id]},
            before_send=lambda: self._check_item(parent_id, folder=True),
        )

    def trash(self) -> None:
        """Trash only the uniquely named test root, even after an interrupted create."""
        item = self.drive.get(self.target_id)
        if item is None or item.trashed:
            return
        self.drive.update_metadata(
            self.target_id,
            trashed=True,
            before_send=lambda: self._check_root(
                self.drive.get(self.target_id), allow_trashed=True
            ),
        )


@contextmanager
def _new_case(sandbox: _Sandbox, parent_id: str, tmp_path: Path) -> Iterator[DriveCase]:
    sandbox.check_parent(parent_id)
    name = f"nimbo-e2e-{uuid4().hex}"
    file_id = sandbox.drive.generate_ids(1)[0]
    source = tmp_path / "source"
    (source / "Projects").mkdir(parents=True)
    (source / "Projects/keep.txt").write_bytes(b"keep")
    write_sentinel(source, str(uuid4()))
    config = replace(
        sandbox.config,
        source=SourceConfig(source, FOLDERS, require_mount=False),
        target=TargetConfig(file_id),
        safety=SafetyConfig(max_deletes=1),
        transfer=TransferConfig(parallel_uploads=1, chunk_mib=16),
        exclude=(),
    )
    case = DriveCase(
        sandbox, file_id, parent_id, name, Paths(tmp_path / "config", tmp_path / "state"), config
    )
    try:
        item = sandbox.drive.create_folder(
            file_id=file_id,
            name=name,
            parent_id=parent_id,
            properties={},
            before_send=lambda: sandbox.check_parent(parent_id),
        )
        case._check_root(item)
        yield case
    finally:
        case.trash()


@pytest.fixture
def drive_case(drive_sandbox: _Sandbox, tmp_path: Path) -> Iterator[DriveCase]:
    """Create a fresh test target directly under the sandbox folder."""
    with _new_case(drive_sandbox, drive_sandbox.folder_id, tmp_path) as case:
        yield case


@pytest.fixture
def computers_case(drive_sandbox: _Sandbox, tmp_path: Path) -> Iterator[DriveCase]:
    """Create a fresh test target under the supplied Computers sandbox folder."""
    with _new_case(drive_sandbox, _folder_id("NIMBO_E2E_COMPUTERS_FOLDER_ID"), tmp_path) as case:
        yield case
