"""In-memory Drive records and atomic mutations under the shared lock."""

from __future__ import annotations

import hashlib
import re
from _thread import RLock
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from time import time
from typing import Any

from nimbo.core.model import (
    FOLDER_MIME,
    GOOGLE_APPS_PREFIX,
    MAX_DRIVE_MS,
    ms_from_rfc3339,
    rfc3339_from_ms,
)

BINARY_MIME = "application/octet-stream"
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_MIME_TYPE = re.compile(r"[-!#$%&'*+.^_`|~0-9A-Za-z]+/[-!#$%&'*+.^_`|~0-9A-Za-z]+")


class DriveAPIError(Exception):
    """A Drive error with its HTTP status, reason, and message."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    @classmethod
    def bad_request(cls, message: str) -> DriveAPIError:
        """Return a malformed-request error with Drive's badRequest reason."""
        return cls(400, "badRequest", message)

    @classmethod
    def not_found(cls, file_id: str) -> DriveAPIError:
        """Return the standard error for an unavailable file or folder."""
        return cls(404, "notFound", f"File not found: {file_id}.")


@dataclass(frozen=True, slots=True)
class FakeFile:
    """A copy of a Drive file, with binary content and the effective trash state."""

    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    modified_ms: int
    properties: dict[str, str] = field(default_factory=dict)
    explicitly_trashed: bool = False
    trashed: bool = False
    device: bool = False
    content: bytes = b""
    md5: str | None = None

    @property
    def is_folder(self) -> bool:
        """Whether the file has the Drive folder MIME type."""
        return self.mime_type == FOLDER_MIME

    @property
    def is_binary(self) -> bool:
        """Whether the resource can carry byte content."""
        return not self.mime_type.startswith(GOOGLE_APPS_PREFIX)

    @property
    def size(self) -> int | None:
        """The binary byte count, or None when Drive omits size."""
        return len(self.content) if self.is_binary else None

    @property
    def parent_id(self) -> str | None:
        """The sole parent ID, if any."""
        return self.parents[0] if self.parents else None

    def as_api(self) -> dict[str, Any]:
        """Return the stored fields as a Drive v3 File resource."""
        data = {
            "kind": "drive#file",
            "id": self.id,
            "name": self.name,
            "mimeType": self.mime_type,
            "parents": list(self.parents),
            "modifiedTime": rfc3339_from_ms(self.modified_ms),
            "properties": dict(self.properties),
            "trashed": self.trashed,
            "explicitlyTrashed": self.explicitly_trashed,
        }
        if self.is_binary:
            data.update(size=str(len(self.content)), md5Checksum=self.md5)
        return data

    def _with_content(self, content: bytes) -> FakeFile:
        return replace(
            self, content=content, md5=hashlib.md5(content, usedforsecurity=False).hexdigest()
        )


class DriveState:
    """Own file, change, clock and limit state accessed with one reentrant lock."""

    def __init__(
        self,
        clock: Callable[[], float] | None,
        quota_limit: int | None,
        daily_upload_limit: int | None,
        writes_per_second: float | None,
    ) -> None:
        self.lock = RLock()
        self._clock = time if clock is None else clock
        self.delay = 0.0
        self.quota_limit = quota_limit
        self.daily_upload_limit = daily_upload_limit
        self.writes_per_second = writes_per_second
        self.writes = 0
        self._recent_writes: deque[float] = deque()
        self._day: int | None = None
        self._uploaded = 0
        self._serial = 0
        self._generated_ids: set[str] = set()
        self._used_ids: set[str] = set()
        self.files: dict[str, FakeFile] = {}
        self._children: dict[str | None, dict[str, None]] = {}
        self.changes: list[dict[str, Any]] = []
        self.root_id = self._new_id()
        root = FakeFile(self.root_id, "My Drive", FOLDER_MIME, (), self.millis())
        self._store(root, None)

    def now(self) -> float:
        """Return the injected clock plus the cumulative scripted delay."""
        with self.lock:
            return self._clock() + self.delay

    def millis(self) -> int:
        """Return Drive's current timestamp as milliseconds."""
        return int(self.now() * 1000)

    def resolve_id(self, file_id: str) -> str:
        """Resolve Drive's special root alias to the canonical ID."""
        return self.root_id if file_id == "root" else file_id

    def require(self, file_id: str) -> FakeFile:
        """Return a file, raising Drive's 404 when the ID is absent."""
        item = self.files.get(self.resolve_id(file_id))
        if item is None:
            raise DriveAPIError.not_found(file_id)
        return item

    def _parent(self, parent_id: str, *, placing: bool = False) -> FakeFile:
        parent = self.require(parent_id)
        if not parent.is_folder or parent.trashed:
            raise DriveAPIError.not_found(parent_id)
        if placing and parent.device:
            raise DriveAPIError.bad_request("Cannot add a direct child to a device folder.")
        return parent

    def _new_id(self) -> str:
        self._serial += 1
        return f"fake-file-{self._serial:010d}"

    def generate_ids(self, count: int) -> list[str]:
        """Reserve count unique IDs for later creates."""
        ids = [self._new_id() for _ in range(count)]
        self._generated_ids.update(ids)
        return ids

    def seed(
        self,
        name: str,
        parent: str,
        mime_type: str,
        *,
        content: bytes = b"",
        modified_ms: int | None = None,
        properties: Mapping[str, str] | None = None,
        device: bool = False,
    ) -> str:
        """Insert a fixture file and report its ID without charging API limits."""
        parent_id = self._parent(parent).id
        item = FakeFile(
            self._new_id(),
            name,
            mime_type,
            (parent_id,),
            self.millis() if modified_ms is None else modified_ms,
            dict(properties or {}),
            device=device,
        )
        if item.is_binary:
            item = item._with_content(content)
        self._store(item, None)
        self._record(item.id, item)
        return item.id

    def _subtree(self, parent_id: str) -> Iterator[str]:
        pending = [parent_id]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(reversed(self._children.get(item, {})))

    def snapshot(self, root_id: str) -> dict[str, tuple[bool, int, str | None]]:
        """Return the first non-trashed item for each relative path below root_id."""
        root = self.require(root_id)
        if not root.is_folder:
            raise ValueError("Snapshot root must be a folder")
        result: dict[str, tuple[bool, int, str | None]] = {}
        pending = deque([(root, "")])
        while pending:
            parent, prefix = pending.popleft()
            if parent.trashed:
                continue
            for child_id in self._children.get(parent.id, {}):
                item = self.files[child_id]
                if item.trashed:
                    continue
                rel = prefix + item.name
                result.setdefault(rel, (item.is_folder, item.size or 0, item.md5))
                if item.is_folder:
                    pending.append((item, rel + "/"))
        return result

    def find(self, parent_id: str, name: str) -> list[FakeFile]:
        """Return copies of matching direct children, including trashed matches."""
        return [
            self.inspect(item.id)
            for child in self._children.get(self.resolve_id(parent_id), {})
            if (item := self.files[child]).name == name
        ]

    def inspect(self, file_id: str) -> FakeFile:
        """Return a detached snapshot, raising KeyError for an absent ID."""
        item = self.files[self.resolve_id(file_id)]
        return replace(item, properties=dict(item.properties))

    def overwrite(self, file_id: str, content: bytes, *, keep_modified: bool) -> None:
        """Set binary fixture bytes, bypassing limits and optionally preserving mtime."""
        old = self.files[self.resolve_id(file_id)]
        if not old.is_binary:
            raise ValueError("Only binary files have content")
        item = old._with_content(content)
        if not keep_modified:
            item = replace(item, modified_ms=self.millis())
        self._store(item, old)
        self._record(item.id, item)

    def _store(self, item: FakeFile, old: FakeFile | None) -> None:
        if old is None:
            self._used_ids.add(item.id)
        elif old.parent_id != item.parent_id:
            del self._children[old.parent_id][old.id]
        self.files[item.id] = item
        self._children.setdefault(item.parent_id, {})[item.id] = None

    def _record(self, file_id: str, item: FakeFile | None) -> None:
        entry = {
            "kind": "drive#change",
            "changeType": "file",
            "fileId": file_id,
            "removed": item is None,
            "time": rfc3339_from_ms(self.millis()),
        }
        if item is not None:
            entry["file"] = item.as_api()
        self.changes.append(entry)

    def _create_base(self, metadata: dict[str, Any], media_type: str | None) -> FakeFile:
        file_id = metadata.get("id", "")
        if "id" in metadata:
            if not isinstance(file_id, str):
                raise DriveAPIError.bad_request("Invalid file ID.")
            if self.resolve_id(file_id) in self._used_ids:
                raise DriveAPIError(409, "fileIdInUse", "The file ID is already in use.")
            if file_id not in self._generated_ids:
                raise DriveAPIError.bad_request("File ID was not generated by files.generateIds.")
        parents = metadata.get("parents", [self.root_id])
        if (
            not isinstance(parents, list)
            or len(parents) > 1
            or any(not isinstance(parent, str) for parent in parents)
        ):
            raise DriveAPIError.bad_request("A file can have only one parent.")
        parent = self._parent(parents[0] if parents else self.root_id, placing=True)
        mime_type = _mime_type(metadata.get("mimeType", media_type or BINARY_MIME))
        return FakeFile(file_id, "Untitled", mime_type, (parent.id,), self.millis())

    def _move(
        self, item: FakeFile, add_parents: str | None, remove_parents: str | None
    ) -> tuple[str, ...]:
        remove = {self.resolve_id(parent) for parent in _parent_ids(remove_parents)}
        add = tuple(dict.fromkeys(self.resolve_id(parent) for parent in _parent_ids(add_parents)))
        for parent_id in add:
            parent = self._parent(parent_id, placing=parent_id not in item.parents)
            ancestor: FakeFile | None = parent
            while ancestor is not None:
                if ancestor.id == item.id:
                    raise DriveAPIError(403, "invalidParent", "Cannot move a folder into itself.")
                ancestor = self.files.get(ancestor.parent_id) if ancestor.parent_id else None
        parents = tuple(dict.fromkeys([*(p for p in item.parents if p not in remove), *add]))
        if len(parents) > 1:
            raise DriveAPIError.bad_request("A file can have only one parent.")
        return parents

    def prepare(
        self,
        metadata: dict[str, Any],
        file_id: str | None = None,
        *,
        media_type: str | None = None,
        add_parents: str | None = None,
        remove_parents: str | None = None,
    ) -> FakeFile:
        """Validate metadata and compute the candidate without changing stored files."""
        allowed = {"name", "modifiedTime", "properties", "trashed", "mimeType"}
        if file_id is None:
            allowed.update(("id", "parents"))
        if unknown := metadata.keys() - allowed:
            raise DriveAPIError.bad_request(f"Unsupported file field: {min(unknown)}.")
        if media_type is not None:
            _mime_type(media_type)
        item = self._create_base(metadata, media_type) if file_id is None else self.require(file_id)
        if item.id == self.root_id:
            raise DriveAPIError.bad_request("Cannot change the Drive root folder.")
        mime_type = _mime_type(metadata.get("mimeType", item.mime_type))
        if mime_type != item.mime_type and (
            media_type is None or not item.is_binary or mime_type.startswith(GOOGLE_APPS_PREFIX)
        ):
            raise DriveAPIError.bad_request("Cannot change MIME type in this request.")
        if file_id is None and (add_parents is not None or remove_parents is not None):
            raise DriveAPIError.bad_request("Set parents in the create metadata.")
        parents = self._move(item, add_parents, remove_parents)
        name = metadata.get("name", item.name)
        explicitly_trashed = metadata.get("trashed", item.explicitly_trashed)
        modified_ms = (
            _modified_ms(metadata["modifiedTime"])
            if "modifiedTime" in metadata
            else item.modified_ms
        )
        if not isinstance(name, str) or not isinstance(explicitly_trashed, bool):
            raise DriveAPIError.bad_request("Invalid name or trashed field.")
        properties = _merge_properties(item.properties, metadata.get("properties", {}))
        trashed = explicitly_trashed or bool(parents and self.files[parents[0]].trashed)
        return replace(
            item,
            name=name,
            mime_type=mime_type,
            modified_ms=modified_ms,
            properties=properties,
            parents=parents,
            explicitly_trashed=explicitly_trashed,
            trashed=trashed,
        )

    def commit(self, item: FakeFile, *, content: bytes | None = None) -> FakeFile:
        """Store a validated candidate and append its change, including implicit trash changes."""
        old = self.files.get(item.id)
        if not item.id:
            item = replace(item, id=self._new_id())
        if item.is_binary and (content is not None or old is None):
            item = item._with_content(b"" if content is None else content)
        self._store(item, old)
        self._record(item.id, item)
        if old is not None and old.trashed != item.trashed and item.is_folder:
            children = self._subtree(item.id)
            next(children)
            for file_id in children:
                child = self.files[file_id]
                trashed = child.explicitly_trashed or bool(
                    child.parent_id and self.files[child.parent_id].trashed
                )
                if trashed != child.trashed:
                    self.files[file_id] = child = replace(child, trashed=trashed)
                    self._record(file_id, child)
        return item

    def delete(self, file_id: str) -> None:
        """Permanently remove an item and its descendants, recording removals."""
        item = self.require(file_id)
        if item.id == self.root_id:
            raise DriveAPIError.bad_request("Cannot remove the Drive root folder.")
        self.check_write()
        for child_id in reversed(list(self._subtree(item.id))):
            child = self.files.pop(child_id)
            del self._children[child.parent_id][child.id]
            self._children.pop(child.id, None)
            self._record(child.id, None)
        self.record_write()

    def storage(self) -> dict[str, str]:
        """Report all storage counters in the JSON string format used by Drive."""
        usage = trash = 0
        for item in self.files.values():
            size = item.size or 0
            usage += size
            if item.trashed:
                trash += size
        result = {"usage": str(usage), "usageInDrive": str(usage), "usageInDriveTrash": str(trash)}
        if self.quota_limit is not None:
            result["limit"] = str(self.quota_limit)
        return result

    def check_upload(self, item: FakeFile, size: int) -> None:
        """Check binary type, projected storage and the fake day's upload allowance."""
        if not item.is_binary:
            raise DriveAPIError.bad_request("Only binary files accept media.")
        old_size = item.size or 0
        if self.quota_limit is not None:
            projected = sum(file.size or 0 for file in self.files.values()) - old_size + size
            if projected > self.quota_limit:
                raise DriveAPIError(
                    403, "storageQuotaExceeded", "The Drive storage quota was exceeded."
                )
        self._reset_upload_day()
        if self.daily_upload_limit is not None and self._uploaded + size > self.daily_upload_limit:
            raise DriveAPIError(403, "uploadLimitExceeded", "The daily upload limit was exceeded.")

    def _reset_upload_day(self) -> None:
        day = int(self.now() // 86_400)
        if day != self._day:
            self._day = day
            self._uploaded = 0

    def record_upload(self, size: int) -> None:
        """Charge the completed upload to the current fake day."""
        self._reset_upload_day()
        self._uploaded += size

    def check_write(self) -> None:
        """Reject a write when the sliding one-second window is at capacity."""
        if self.writes_per_second is None:
            return
        now = self.now()
        while self._recent_writes and self._recent_writes[0] <= now - 1:
            self._recent_writes.popleft()
        if len(self._recent_writes) >= self.writes_per_second:
            raise DriveAPIError(
                403, "userRateLimitExceeded", "The per-user write rate was exceeded."
            )

    def record_write(self) -> None:
        """Count a successfully applied HTTP Drive mutation."""
        self.writes += 1
        if self.writes_per_second is not None:
            self._recent_writes.append(self.now())


def _parent_ids(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    ids = tuple(value.split(","))
    if not all(ids):
        raise DriveAPIError.bad_request("Invalid parent IDs.")
    return ids


def _mime_type(value: object) -> str:
    if not isinstance(value, str) or not _MIME_TYPE.fullmatch(value):
        raise DriveAPIError.bad_request("Invalid MIME type.")
    return value


def _modified_ms(value: object) -> int:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise DriveAPIError.bad_request("Invalid modifiedTime.")
    try:
        millis = ms_from_rfc3339(value)
    except (ValueError, OverflowError) as err:
        raise DriveAPIError.bad_request("Invalid modifiedTime.") from err
    if not 0 <= millis <= MAX_DRIVE_MS:
        raise DriveAPIError.bad_request("Invalid modifiedTime.")
    return millis


def _merge_properties(properties: dict[str, str], update: object) -> dict[str, str]:
    if not isinstance(update, dict):
        raise DriveAPIError.bad_request("Invalid file properties.")
    result = dict(properties)
    for key, value in update.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, str):
            result[key] = value
        else:
            raise DriveAPIError.bad_request("Invalid file property value.")
    return result
