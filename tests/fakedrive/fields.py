"""Parsing and validation of Drive's partial-response field masks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypeAlias

Mask: TypeAlias = dict[str, "Mask | None"]
Schema: TypeAlias = Mapping[str, "Schema | None"]

FILE: Schema = dict.fromkeys(
    (
        "kind",
        "id",
        "name",
        "mimeType",
        "parents",
        "size",
        "md5Checksum",
        "modifiedTime",
        "trashed",
        "explicitlyTrashed",
        "ownedByMe",
    )
) | {"properties": {}}
FILES: Schema = {"kind": None, "nextPageToken": None, "files": FILE}
ABOUT: Schema = {
    "kind": None,
    "storageQuota": dict.fromkeys(("limit", "usage", "usageInDrive", "usageInDriveTrash")),
    "user": dict.fromkeys(("kind", "displayName", "emailAddress", "permissionId", "me")),
}
CHANGES: Schema = {
    "kind": None,
    "nextPageToken": None,
    "newStartPageToken": None,
    "changes": dict.fromkeys(("kind", "changeType", "fileId", "removed", "time")) | {"file": FILE},
}
START_TOKEN: Schema = dict.fromkeys(("kind", "startPageToken"))
GENERATED_IDS: Schema = dict.fromkeys(("kind", "ids", "space"))

DEFAULT_FILE = "kind,id,name,mimeType"
DEFAULT_FILES = f"kind,nextPageToken,files({DEFAULT_FILE})"
_TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[*,()/])")


class _Parser:
    def __init__(self, fields: str) -> None:
        self.tokens: list[str] = []
        fields = fields.strip()
        pos = 0
        while pos < len(fields):
            match = _TOKEN.match(fields, pos)
            if match is None:
                raise ValueError("Invalid fields selection")
            self.tokens.append(match[1])
            pos = match.end()
        self.pos = 0

    def take(self, token: str) -> bool:
        if self.pos < len(self.tokens) and self.tokens[self.pos] == token:
            self.pos += 1
            return True
        return False

    def selection(self, schema: Schema, *, depth: int = 0) -> Mask:
        mask: Mask = {}
        while True:
            _merge(mask, self.path(schema, depth))
            if not self.take(","):
                return mask

    def path(self, schema: Schema, depth: int) -> Mask:
        if depth > 20 or self.pos == len(self.tokens):
            raise ValueError("Invalid fields selection")
        key = self.tokens[self.pos]
        self.pos += 1
        if key == "*":
            return {key: None}
        if key not in schema:
            raise ValueError(f"Invalid field selection {key}")
        child = schema[key]
        if self.take("/"):
            if child is None:
                raise ValueError(f"Field {key} has no subfields")
            return {key: self.path(child, depth + 1)}
        if self.take("("):
            if child is None:
                raise ValueError(f"Field {key} has no subfields")
            selected = self.selection(child, depth=depth + 1)
            if not self.take(")"):
                raise ValueError("Invalid fields selection")
            return {key: selected}
        return {key: None}


def _merge(target: Mask, selected: Mask) -> None:
    for key, child in selected.items():
        if key not in target or child is None:
            target[key] = child
        elif (existing := target[key]) is not None:
            _merge(existing, child)


def parse(fields: str, schema: Schema) -> Mask:
    """Parse and validate a field mask, raising ValueError for invalid selections."""
    parser = _Parser(fields)
    mask = parser.selection(schema)
    if parser.pos != len(parser.tokens):
        raise ValueError("Invalid fields selection")
    return mask


def select(data: dict[str, Any], mask: Mask) -> dict[str, Any]:
    """Project a resource or list of nested resources onto the requested fields."""
    if "*" in mask:
        return data
    result: dict[str, Any] = {}
    for key, child in mask.items():
        if key not in data:
            continue
        value = data[key]
        if child is not None:
            if isinstance(value, list):
                value = [select(item, child) for item in value]
            else:
                value = select(value, child)
        result[key] = value
    return result
