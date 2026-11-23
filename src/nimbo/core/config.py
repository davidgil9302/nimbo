"""Read, validate and write nimbo's versioned TOML configuration."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

from nimbo.core.errors import ConfigError
from nimbo.core.filters import Matcher
from nimbo.core.fsutil import atomic_write_text
from nimbo.core.model import nfc

SCHEMA_VERSION = 1
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules/",
    "__pycache__/",
    ".venv/",
    ".ipynb_checkpoints/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tmp.drivedownload/",
    ".tmp.driveupload/",
    ".Trash-*/",
    "desktop.ini",
    "Thumbs.db",
    ".DS_Store",
    "~$*",
    "*.crdownload",
)

_FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
_FOLDER_URL_PATH = re.compile(r"/drive/(?:u/[0-9]+/)?folders/([A-Za-z0-9_-]{10,})/?")
_DAILY_AT = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """An absolute local root and ordered NFC folder names, including paused folders."""

    root: Path
    folders: tuple[str, ...]
    paused: tuple[str, ...] = ()
    require_mount: bool = True


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """The Drive folder ID and an optional human-readable label."""

    folder_id: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """The daily run time in local time, formatted as HH:MM."""

    daily_at: str = "20:00"

    @property
    def hour_minute(self) -> tuple[int, int]:
        """Return the schedule's hour and minute as integers."""
        hour, minute = self.daily_at.split(":")
        return int(hour), int(minute)


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    """Deletion thresholds for the whole plan and for individual folders."""

    max_deletes: int = 500
    max_delete_ratio: float = 0.10
    ratio_min_files: int = 20


@dataclass(frozen=True, slots=True)
class TransferConfig:
    """Upload concurrency, transfer sizes in MiB and Drive requests per second."""

    parallel_uploads: int = 4
    chunk_mib: int = 32
    multipart_max_mib: int = 8
    writes_per_second: float = 3.0
    reads_per_second: float = 10.0


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """The credentials of the user's Google OAuth desktop client."""

    client_id: str
    client_secret: str


@dataclass(frozen=True, slots=True)
class Config:
    """The complete backup configuration with schema, exclusions and UI preferences."""

    source: SourceConfig
    target: TargetConfig
    oauth: OAuthConfig
    schedule: ScheduleConfig = ScheduleConfig()
    safety: SafetyConfig = SafetyConfig()
    transfer: TransferConfig = TransferConfig()
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    exclude_ignore_case: bool = True
    service: str = "Google Drive"
    language: str | None = None
    schema: int = SCHEMA_VERSION

    @property
    def active_folders(self) -> tuple[str, ...]:
        """Return unpaused source folders in their original order."""
        paused = set(self.source.paused)
        return tuple(folder for folder in self.source.folders if folder not in paused)

    def matcher(self) -> Matcher:
        """Compile this configuration's exclusions, raising ConfigError for invalid patterns."""
        return Matcher(self.exclude, ignore_case=self.exclude_ignore_case)


_T = TypeVar("_T")
_TomlValue = str | Path | bool | int | float | tuple[str, ...]


class _Table:
    def __init__(self, data: dict[str, object], prefix: str, keys: set[str]) -> None:
        self.data = data
        self.prefix = prefix
        for key in data:
            if key not in keys:
                raise ConfigError(prefix + key)

    def section(self, key: str, keys: set[str], *, required: bool = False) -> _Table:
        data = self.get(key, dict, default=None if required else {})
        return _Table(data, self.prefix + key + ".", keys)

    def get(self, key: str, kind: type[_T] | tuple[type[_T], ...], default: _T | None = None) -> _T:
        value = self.data.get(key, default)
        if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
            raise ConfigError(self.prefix + key)
        return value

    def strings(self, key: str, default: tuple[str, ...] | None = None) -> tuple[str, ...]:
        values = self.get(key, list, default=None if default is None else list(default))
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise ConfigError(f"{self.prefix}{key}[{index}]")
        return tuple(values)

    def integer(self, key: str, default: int, low: int = 0, high: int | None = None) -> int:
        value = self.get(key, int, default)
        if value < low or (high is not None and value > high):
            raise ConfigError(self.prefix + key)
        return value

    def number(self, key: str, default: float, low: float, high: float) -> float:
        value = self.get(key, (int, float), default)
        if not low <= value <= high:
            raise ConfigError(self.prefix + key)
        return float(value)


def _load_source(source: _Table) -> SourceConfig:
    root = source.get("root", str)
    path = Path(root)
    if not path.is_absolute() or "\0" in root:
        raise ConfigError("source.root")
    folders = tuple(map(nfc, source.strings("folders")))
    if not folders:
        raise ConfigError("source.folders")
    seen: set[str] = set()
    for index, name in enumerate(folders):
        if name in {"", ".", ".."} or "/" in name or "\0" in name or name in seen:
            raise ConfigError(f"source.folders[{index}]")
        seen.add(name)
    paused = tuple(map(nfc, source.strings("paused", ())))
    for index, name in enumerate(paused):
        if name not in seen:
            raise ConfigError(f"source.paused[{index}]")
    return SourceConfig(path, folders, paused, source.get("require_mount", bool, True))


def _load_transfer(transfer: _Table) -> TransferConfig:
    defaults = TransferConfig()
    chunk = transfer.integer("chunk_mib", defaults.chunk_mib, 1, 256)
    return TransferConfig(
        parallel_uploads=transfer.integer("parallel_uploads", defaults.parallel_uploads, 1, 16),
        chunk_mib=chunk,
        multipart_max_mib=transfer.integer(
            "multipart_max_mib", defaults.multipart_max_mib, 1, chunk
        ),
        writes_per_second=transfer.number("writes_per_second", defaults.writes_per_second, 0.1, 20),
        reads_per_second=transfer.number("reads_per_second", defaults.reads_per_second, 0.1, 50),
    )


def load_config(path: Path) -> Config:
    """Read UTF-8 TOML, apply defaults and validate its schema and values.

    Raise ConfigError for unreadable or invalid TOML and for validation failures.
    For an invalid field, the exception detail identifies the TOML key.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        raise ConfigError("config") from None

    top = _Table(data, "", {field.name for field in fields(Config)})
    schema = top.get("schema", int, SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ConfigError("schema")
    language = top.get("language", str) if "language" in data else None
    if language not in (None, "es", "en"):
        raise ConfigError("language")

    source = _load_source(
        top.section("source", {"root", "folders", "paused", "require_mount"}, required=True)
    )
    target = top.section("target", {"folder_id", "label"}, required=True)
    folder_id = target.get("folder_id", str)
    if not _FOLDER_ID.fullmatch(folder_id):
        raise ConfigError("target.folder_id")
    oauth = top.section("oauth", {"client_id", "client_secret"}, required=True)
    client_id = oauth.get("client_id", str)
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ConfigError("oauth.client_id")
    client_secret = oauth.get("client_secret", str)
    if not client_secret:
        raise ConfigError("oauth.client_secret")

    schedule = top.section("schedule", {"daily_at"})
    daily_at = schedule.get("daily_at", str, ScheduleConfig().daily_at)
    if not _DAILY_AT.fullmatch(daily_at):
        raise ConfigError("schedule.daily_at")

    safety = top.section("safety", {"max_deletes", "max_delete_ratio", "ratio_min_files"})
    defaults = SafetyConfig()
    ratio = safety.number("max_delete_ratio", defaults.max_delete_ratio, 0, 1)
    if ratio == 0:
        raise ConfigError("safety.max_delete_ratio")

    cfg = Config(
        source=source,
        target=TargetConfig(folder_id, target.get("label", str, "")),
        oauth=OAuthConfig(client_id, client_secret),
        schedule=ScheduleConfig(daily_at),
        safety=SafetyConfig(
            safety.integer("max_deletes", defaults.max_deletes),
            ratio,
            safety.integer("ratio_min_files", defaults.ratio_min_files),
        ),
        transfer=_load_transfer(
            top.section(
                "transfer",
                {
                    "parallel_uploads",
                    "chunk_mib",
                    "multipart_max_mib",
                    "writes_per_second",
                    "reads_per_second",
                },
            )
        ),
        exclude=top.strings("exclude", DEFAULT_EXCLUDES),
        exclude_ignore_case=top.get("exclude_ignore_case", bool, True),
        service=top.get("service", str, "Google Drive"),
        language=language,
        schema=schema,
    )
    cfg.matcher()
    return cfg


def _toml_value(value: _TomlValue) -> str:
    if isinstance(value, (str, Path)):
        # JSON and TOML basic strings share these escapes. TOML also requires escaping DEL.
        return json.dumps(str(value), ensure_ascii=False).replace("\x7f", r"\u007f")
    if isinstance(value, tuple):
        return "[" + ", ".join(map(_toml_value, value)) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def dumps_config(cfg: Config) -> str:
    """Serialize a valid configuration to deterministic TOML with Unicode text preserved."""
    top: dict[str, _TomlValue] = {"schema": cfg.schema, "service": cfg.service}
    if cfg.language is not None:
        top["language"] = cfg.language
    top["exclude_ignore_case"] = cfg.exclude_ignore_case
    top["exclude"] = cfg.exclude
    lines = [f"{key} = {_toml_value(value)}" for key, value in top.items()]
    for name in ("source", "target", "schedule", "safety", "transfer", "oauth"):
        lines.extend(("", f"[{name}]"))
        section = asdict(getattr(cfg, name))
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in section.items())
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path) -> None:
    """Atomically save UTF-8 TOML with mode 0600, propagating filesystem errors."""
    atomic_write_text(path, dumps_config(cfg), mode=0o600)


def parse_folder_id(text: str) -> str:
    """Return the ID from a bare Drive folder ID or a supported HTTPS Drive link.

    Raise ConfigError when the input is outside those forms or contains an invalid ID.
    """
    text = text.strip()
    if _FOLDER_ID.fullmatch(text):
        return text
    if not re.search(r"[\x00-\x20\x7f]", text):
        try:
            url = urlsplit(text)
        except ValueError:
            raise ConfigError("target.folder_id") from None
        if url.scheme == "https" and url.netloc.lower() == "drive.google.com":
            path = _FOLDER_URL_PATH.fullmatch(url.path)
            if path is not None:
                return path[1]
            if url.path == "/open":
                ids = parse_qs(url.query, keep_blank_values=True).get("id", [])
                if len(ids) == 1 and _FOLDER_ID.fullmatch(ids[0]):
                    return ids[0]
    raise ConfigError("target.folder_id")
