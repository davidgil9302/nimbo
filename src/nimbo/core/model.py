"""Shared data contracts for nimbo.

Every module exchanges these types. Treat them as a stable API: changing a field
here is a cross-cutting change that needs the maintainer's approval.

Path conventions
----------------
A *rel* path is relative to the configured source root, uses ``/`` as the
separator, is NFC-normalized, never starts or ends with ``/`` and the empty
string ``""`` denotes the root itself (the Drive target folder). Example:
``"DOC/notes/a.txt"``. Remote names that can never exist locally (they contain
``/`` or NUL) are mapped to a rel component starting with ``INVALID_NAME_PREFIX``
so they never match a local entry.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any

# --------------------------------------------------------------------------- constants
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
OWNER_PROPERTY = "nimbo"  # Drive `properties` key; value = source-root UUID
FOREIGN_ITEM_KEPT = "foreign-item-kept"
SENTINEL_NAME = ".nimbo-root"  # marker file at the source root
PART_SUFFIX = ".nimbo-part"  # temporary suffix for downloads in progress
NOBACKUP_NAME = ".nobackup"  # a directory containing this file is excluded
INVALID_NAME_PREFIX = "\x00"  # rel component for remote names that cannot exist locally
FILE_FIELDS = "id,name,mimeType,parents,size,md5Checksum,modifiedTime,properties,trashed,ownedByMe"
MAX_DRIVE_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers
def nfc(text: str) -> str:
    """Unicode NFC normalization, used for every path comparison."""
    return unicodedata.normalize("NFC", text)


def drive_ms(mtime_ns: int) -> int:
    """Local ``st_mtime_ns`` -> epoch milliseconds as Drive stores them (floored, clamped)."""
    return min(max(mtime_ns // 1_000_000, 0), MAX_DRIVE_MS)


def rfc3339_from_ms(ms: int) -> str:
    """Epoch milliseconds -> ``2026-11-23T20:03:15.123Z`` (the format Drive uses)."""
    ms = min(max(ms, 0), MAX_DRIVE_MS)
    moment = _EPOCH + timedelta(milliseconds=ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def ms_from_rfc3339(text: str) -> int:
    """RFC 3339 timestamp (as returned by Drive) -> epoch milliseconds (floored)."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def rel_join(parent_rel: str, name: str) -> str:
    return name if parent_rel == "" else f"{parent_rel}/{name}"


def rel_parent(rel: str) -> str:
    return rel.rpartition("/")[0]


def rel_name(rel: str) -> str:
    return rel.rpartition("/")[2]


def rel_top(rel: str) -> str:
    """Top-level folder of a rel path (``"DOC/a/b"`` -> ``"DOC"``)."""
    return rel.split("/", 1)[0]


def rel_depth(rel: str) -> int:
    return 0 if rel == "" else rel.count("/") + 1


def is_under(rel: str, ancestor: str) -> bool:
    """True if ``rel`` equals ``ancestor`` or lies inside it."""
    return ancestor == "" or rel == ancestor or rel.startswith(ancestor + "/")


# --------------------------------------------------------------------------- local side
FileIdentity = tuple[int, int]  # st_ino, st_ctime_ns


@dataclass(frozen=True, slots=True)
class LocalEntry:
    rel: str  # NFC rel path ("DOC/a.txt")
    is_dir: bool
    size: int  # bytes; 0 for directories
    mtime_ns: int  # st_mtime_ns
    disk: str | None = None  # exact on-disk rel path when it differs from ``rel`` (non-NFC names)
    ino: int | None = None
    ctime_ns: int | None = None

    @property
    def identity(self) -> FileIdentity | None:
        if self.ino is None or self.ctime_ns is None:
            return None
        return self.ino, self.ctime_ns

    @property
    def disk_rel(self) -> str:
        """Relative path to open on disk (``rel`` unless the real name is not NFC)."""
        return self.rel if self.disk is None else self.disk


@dataclass(frozen=True, slots=True)
class FileVersion:
    """Local identity and the corresponding Drive checksum."""

    identity: FileIdentity
    md5: str | None


@dataclass(frozen=True, slots=True)
class ScanProblem:
    code: str  # "unreadable" | "invalid-name" | "nfc-collision" | "special-file" | "symlink"
    rel: str
    detail: str = ""


@dataclass(slots=True)
class ScanResult:
    entries: dict[str, LocalEntry]
    problems: list[ScanProblem] = field(default_factory=list)
    excluded: int = 0
    missing_folders: list[str] = field(default_factory=list)
    empty_folders: list[str] = field(default_factory=list)
    # NFC rel paths whose local content is unknown or deliberately excluded (directories with a
    # .nobackup marker, unreadable directories/files). The planner must never touch remote items
    # at or below these paths: absence from ``entries`` does NOT mean "deleted" for them.
    protected: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- remote side
@dataclass(frozen=True, slots=True)
class RemoteFile:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    size: int | None = None
    md5: str | None = None
    modified_ms: int | None = None
    properties: Mapping[str, str] = field(default_factory=dict)
    trashed: bool = False
    owned_by_me: bool | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == SHORTCUT_MIME

    @property
    def is_google_native(self) -> bool:
        """Docs, Sheets, Slides, shortcuts...: nimbo never touches these."""
        return self.mime_type.startswith(GOOGLE_APPS_PREFIX) and not self.is_folder

    @property
    def owner_root(self) -> str | None:
        return self.properties.get(OWNER_PROPERTY)

    def protected_from(self, root_uuid: str) -> bool:
        return self.owned_by_me is False or self.owner_root not in (None, root_uuid)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> RemoteFile:
        """Parse a Drive v3 ``File`` resource (only the fields in ``FILE_FIELDS``)."""
        size = data.get("size")
        md5 = data.get("md5Checksum")
        modified = data.get("modifiedTime")
        props = data.get("properties") or {}
        owned = data.get("ownedByMe")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            mime_type=str(data.get("mimeType", "")),
            parents=tuple(str(p) for p in (data.get("parents") or ())),
            size=int(size) if size is not None else None,
            md5=str(md5) if md5 else None,
            modified_ms=ms_from_rfc3339(str(modified)) if modified else None,
            properties={str(k): str(v) for k, v in props.items()},
            trashed=bool(data.get("trashed", False)),
            owned_by_me=owned if isinstance(owned, bool) else None,
        )


@dataclass(frozen=True, slots=True)
class About:
    email: str | None
    limit: int | None  # None = unlimited
    usage: int
    usage_in_trash: int

    @property
    def free(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.usage, 0)


@dataclass(frozen=True, slots=True)
class Change:
    file_id: str
    removed: bool
    file: RemoteFile | None


@dataclass(frozen=True, slots=True)
class ChangesPage:
    changes: tuple[Change, ...]
    next_page_token: str | None
    new_start_page_token: str | None


# --------------------------------------------------------------------------- plans
class OpKind(StrEnum):
    MKDIR = "mkdir"  # create folder (pre-generated id)
    UPLOAD = "upload"  # create file (pre-generated id)
    UPDATE = "update"  # new content revision of an existing file id
    TOUCH = "touch"  # only set modifiedTime (content identical)
    MOVE = "move"  # change parent and/or name (+ modifiedTime) of an existing id
    ADOPT = "adopt"  # mark an identical, unmanaged remote file as ours (properties only)
    TRASH = "trash"  # trashed=true (never a permanent delete)


class Phase(IntEnum):
    MKDIR = 0
    MOVE = 1
    CONTENT = 2
    TRASH = 3


PHASE_OF: dict[OpKind, Phase] = {
    OpKind.MKDIR: Phase.MKDIR,
    OpKind.MOVE: Phase.MOVE,
    OpKind.UPLOAD: Phase.CONTENT,
    OpKind.UPDATE: Phase.CONTENT,
    OpKind.TOUCH: Phase.CONTENT,
    OpKind.ADOPT: Phase.CONTENT,
    OpKind.TRASH: Phase.TRASH,
}


class OpState(StrEnum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"  # superseded after crash reconciliation


# Machine-readable reasons attached to ops (also i18n keys "reason.<value>").
REASONS = (
    "new",
    "changed",
    "metadata",
    "renamed",
    "adopted",
    "deleted-locally",
    "extraneous",
    "duplicate",
    "type-conflict",
    "invalid-name",
)


@dataclass(slots=True)
class Op:
    kind: OpKind
    rel: str  # destination rel (MOVE: new rel; TRASH: rel being trashed)
    reason: str  # one of REASONS
    file_id: str | None = None  # existing id; MKDIR/UPLOAD receive a pre-generated id
    parent_rel: str | None = None  # MKDIR/UPLOAD/MOVE destination parent ("" = target root)
    parent_id: str | None = None  # resolved before execution
    src_rel: str | None = None  # MOVE: previous rel
    src_parent_id: str | None = None  # MOVE: current parent id to remove
    name: str | None = None  # MKDIR/UPLOAD/MOVE: destination name (NFC)
    size: int = 0  # UPLOAD/UPDATE: bytes to send
    mtime_ns: int = 0  # UPLOAD/UPDATE/TOUCH/MOVE: local mtime to store on Drive
    md5: str | None = None  # local md5 when known
    affected_files: int = 0  # TRASH: files removed, descendants included
    disk_rel: str | None = None  # UPLOAD/UPDATE: on-disk rel path when it differs from ``rel``
    db_id: int | None = None  # row id once persisted
    trash_ids_digest: str | None = None  # folder TRASH: digest of all covered Drive IDs
    identity: FileIdentity | None = None  # scanned version for this run's file writes

    @property
    def phase(self) -> Phase:
        return PHASE_OF[self.kind]


@dataclass(frozen=True, slots=True)
class PlanIssue:
    code: str  # e.g. "duplicate-unmanaged", "google-native-kept", "foreign-owner"
    rel: str
    detail: str = ""


@dataclass(slots=True)
class PlanStats:
    mkdirs: int = 0
    uploads: int = 0
    upload_bytes: int = 0
    updates: int = 0
    update_bytes: int = 0
    touches: int = 0
    moves: int = 0
    adopts: int = 0
    trashes: int = 0
    trashed_files: int = 0
    per_folder_trashed: dict[str, int] = field(default_factory=dict)
    per_folder_remote_files: dict[str, int] = field(default_factory=dict)  # owned, managed files
    per_folder_pending_files: dict[str, int] = field(default_factory=dict)
    per_folder_pending_bytes: dict[str, int] = field(default_factory=dict)

    @property
    def bytes_to_send(self) -> int:
        return self.upload_bytes + self.update_bytes

    @property
    def total_ops(self) -> int:
        return (
            self.mkdirs
            + self.uploads
            + self.updates
            + self.touches
            + self.moves
            + self.adopts
            + self.trashes
        )


@dataclass(slots=True)
class Plan:
    ops: list[Op]
    stats: PlanStats
    warnings: list[PlanIssue] = field(default_factory=list)
    errors: list[PlanIssue] = field(default_factory=list)
    # rel -> id of every remote folder that exists before execution, *after* the planned
    # folder moves are applied virtually ("" -> target root id). Used to resolve parent ids.
    folder_ids: dict[str, str] = field(default_factory=dict)
    file_versions: dict[str, FileVersion] = field(default_factory=dict)  # Drive ID -> local version

    @property
    def is_empty(self) -> bool:
        return not self.ops

    def fingerprint(self) -> str:
        """Stable hash of the deletions in this plan (binds a user's approval to them).

        Covers each trashed item's rel path, Drive id and affected-file count, encoded as
        canonical JSON so that names containing tabs/newlines cannot collide.
        """
        trashes = sorted(
            (
                [op.rel, op.file_id or "", op.affected_files]
                for op in self.ops
                if op.kind is OpKind.TRASH
            ),
            key=lambda item: (str(item[0]), str(item[1])),
        )
        payload = json.dumps(trashes, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- runs
class RunResult(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # finished, some ops failed (e.g. file in use)
    FAILED = "failed"  # aborted by an error (auth, quota, network, target missing...)
    SKIPPED = "skipped"  # preconditions not met (disk not mounted, offline, locked...)
    NEEDS_CONFIRMATION = "needs_confirmation"  # deletion brake tripped
    CANCELLED = "cancelled"  # stopped by the user / SIGTERM
    PAUSED = "paused"  # Google's daily upload limit reached; resumes next run


@dataclass(slots=True)
class RunSummary:
    run_id: int | None
    result: RunResult
    reason: str = ""  # machine code, also an i18n key ("reason" namespace)
    detail: str = ""
    trigger: str = ""
    started_at: str = ""
    finished_at: str = ""
    mkdirs: int = 0
    uploaded: int = 0
    uploaded_bytes: int = 0
    updated: int = 0
    touched: int = 0
    moved: int = 0
    adopted: int = 0
    trashed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (rel, error code)
    warnings: list[PlanIssue] = field(default_factory=list)
    fingerprint: str | None = None
    # NEEDS_CONFIRMATION: the deletions the user must approve ("rel\taffected_files"), max 500
    trash_preview: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["result"] = self.result.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunSummary:
        values = dict(data)
        values["result"] = RunResult(values["result"])
        values["failed"] = [tuple(item) for item in values.get("failed", [])]
        values["warnings"] = [PlanIssue(**item) for item in values.get("warnings", [])]
        return cls(**values)


@dataclass(frozen=True, slots=True)
class BrakeDecision:
    tripped: bool
    reasons: tuple[str, ...]  # "too_many_deletes", "folder_ratio:<folder>"
    fingerprint: str


@dataclass(slots=True)
class Preview:
    """What a backup would do right now (nothing is changed on Drive)."""

    plan: Plan
    scan_problems: list[ScanProblem]
    about: About | None
    brake: BrakeDecision


@dataclass(slots=True)
class UndoSummary:
    run_id: int | None
    untrashed: int = 0
    restored_files: int = 0
    skipped_existing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (rel, error code)


@dataclass(slots=True)
class AuditReport:
    missing_remote: list[str] = field(default_factory=list)
    extra_remote: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    repaired: RunSummary | None = None


@dataclass(slots=True)
class FolderStat:
    folder: str
    last_ok: str | None = None
    files_local: int = 0
    bytes_local: int = 0
    files_pending: int = 0
    bytes_pending: int = 0
    updated_at: str | None = None


# --------------------------------------------------------------------------- live progress
PROGRESS_PHASES = (
    "preflight",
    "listing",
    "scanning",
    "planning",
    "mkdir",
    "move",
    "transfer",
    "trash",
    "finishing",
)


@dataclass(slots=True)
class ActiveTransfer:
    rel: str
    done: int
    total: int


@dataclass(slots=True)
class Progress:
    """Written by the engine to ``progress.json`` (<= every 500 ms); read by the TUI."""

    run_id: int
    pid: int
    phase: str  # one of PROGRESS_PHASES
    started_at: str
    updated_at: str
    folder: str | None = None
    ops_total: int = 0
    ops_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    speed_bps: float = 0.0
    eta_s: int | None = None
    active: list[ActiveTransfer] = field(default_factory=list)
    speed_history: list[float] = field(default_factory=list)  # last 60 one-second samples

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Progress:
        data = json.loads(text)
        data["active"] = [ActiveTransfer(**item) for item in data.get("active", [])]
        return cls(**data)
