"""Tests for configuration validation and portable TOML serialization."""

import json
import stat
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.config import (
    DEFAULT_EXCLUDES,
    SCHEMA_VERSION,
    Config,
    OAuthConfig,
    SafetyConfig,
    ScheduleConfig,
    SourceConfig,
    TargetConfig,
    TransferConfig,
    dumps_config,
    load_config,
    parse_folder_id,
    save_config,
)
from nimbo.core.errors import ConfigError
from nimbo.core.model import nfc

_FOLDER_ID = "Test_drive-0123"
_CLIENT_ID = "fixture.apps.googleusercontent.com"
_MINIMAL = f'''\
[source]
root = "/media/alice/DATA"
folders = ["Projects", "Photos"]

[target]
folder_id = "{_FOLDER_ID}"

[oauth]
client_id = "{_CLIENT_ID}"
client_secret = "fixture-secret"
'''
_DEFAULTS = Config(
    SourceConfig(Path("/media/alice/DATA"), ("Projects", "Photos")),
    TargetConfig(_FOLDER_ID),
    OAuthConfig(_CLIENT_ID, "fixture-secret"),
)


def _load(tmp_path: Path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _assert_invalid(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, text)
    assert key in exc.value.detail


def test_load_minimal_config_uses_defaults(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _MINIMAL)
    assert cfg == _DEFAULTS
    assert cfg.schema == SCHEMA_VERSION == 1
    assert cfg.source.paused == ()
    assert cfg.source.require_mount is True
    assert cfg.target.label == ""
    assert cfg.schedule == ScheduleConfig("20:00")
    assert cfg.schedule.hour_minute == (20, 0)
    assert cfg.safety == SafetyConfig(500, 0.10, 20)
    assert cfg.transfer == TransferConfig(4, 32, 8, 3.0, 10.0)
    assert cfg.service == "Google Drive"
    assert cfg.language is None
    assert (
        cfg.exclude
        == DEFAULT_EXCLUDES
        == (
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
    )
    assert cfg.exclude_ignore_case
    assert cfg.matcher().excluded("Projects/NODE_MODULES/file", False)


_plain_text = st.text(max_size=35)
_valid_folder = (
    st.text(
        alphabet=st.characters(exclude_characters="/\0", exclude_categories=("Cs",)),
        min_size=1,
        max_size=16,
    )
    .map(nfc)
    .filter(lambda name: name not in (".", ".."))
)


@given(
    extra_folders=st.lists(_valid_folder, max_size=4, unique=True),
    text=_plain_text,
    controls=st.text(alphabet="".join(map(chr, [*range(32), 127])), min_size=1),
    hour=st.integers(0, 23),
    minute=st.integers(0, 59),
    language=st.sampled_from([None, "es", "en"]),
    ignore_case=st.booleans(),
    require_mount=st.booleans(),
    chunk=st.integers(1, 256),
    ratio=st.floats(min_value=0.001, max_value=1.0),
)
def test_roundtrip_dumps_load(
    tmp_path_factory: pytest.TempPathFactory,
    extra_folders: list[str],
    text: str,
    controls: str,
    hour: int,
    minute: int,
    language: str | None,
    ignore_case: bool,
    require_mount: bool,
    chunk: int,
    ratio: float,
) -> None:
    special = 'Été 漢字 " \\ \b\t\n\f\r\x00\x1f\x7f' + controls + text
    first = 'Café " \\'
    folders = (first, *(name for name in extra_folders if name != first))
    cfg = Config(
        source=SourceConfig(Path('/media/alice/Données " \\'), folders, folders[1:], require_mount),
        target=TargetConfig(_FOLDER_ID, special),
        oauth=OAuthConfig(_CLIENT_ID, special),
        schedule=ScheduleConfig(f"{hour:02}:{minute:02}"),
        safety=SafetyConfig(0, ratio, 0),
        transfer=TransferConfig(16, chunk, chunk, 0.1, 50.0),
        exclude=("cache/", "part " + special),
        exclude_ignore_case=ignore_case,
        service=special,
        language=language,
    )
    serialized = dumps_config(cfg)
    assert serialized == dumps_config(cfg)
    assert "Été 漢字" in serialized
    parsed = tomllib.loads(serialized)
    assert parsed["oauth"]["client_secret"] == special
    assert parsed["exclude"] == list(cfg.exclude)
    assert parsed["source"]["folders"] == list(folders)
    assert set(parsed) == {
        "schema",
        "service",
        "exclude_ignore_case",
        "exclude",
        "source",
        "target",
        "schedule",
        "safety",
        "transfer",
        "oauth",
        *([] if language is None else ["language"]),
    }
    path = tmp_path_factory.getbasetemp() / "roundtrip.toml"
    path.write_text(serialized, encoding="utf-8")
    assert load_config(path) == cfg
    assert cfg.schedule.hour_minute == (hour, minute)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("unexpected = 1\n" + _MINIMAL, "unexpected"),
        (_MINIMAL.replace("[source]", "[source]\nother = 1"), "source.other"),
        (_MINIMAL + "\n[safety]\nother = 1", "safety.other"),
        (_MINIMAL + "\n[extras]\nother = 1", "extras"),
    ],
)
def test_unknown_key_rejected_with_path(tmp_path: Path, text: str, key: str) -> None:
    _assert_invalid(tmp_path, text, key)


@pytest.mark.parametrize("schema", ["0", "-1", "2", "true", '"1"', "1.0"])
def test_invalid_schema(tmp_path: Path, schema: str) -> None:
    _assert_invalid(tmp_path, f"schema = {schema}\n" + _MINIMAL, "schema")


@pytest.mark.parametrize("root", ["Projects", "~/Projects", "", 1, "/bad\0root"])
def test_invalid_source_root(tmp_path: Path, root: str | int) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL.replace('"/media/alice/DATA"', json.dumps(root)), "source.root"
    )


@pytest.mark.parametrize(
    ("folders", "key"),
    [
        ([], "source.folders"),
        ("Projects", "source.folders"),
        ([""], "source.folders[0]"),
        (["."], "source.folders[0]"),
        ([".."], "source.folders[0]"),
        (["Projects", "a/b"], "source.folders[1]"),
        (["Projects", 42], "source.folders[1]"),
        (["Projects", "a\0b"], "source.folders[1]"),
        (["Projects", "Projects"], "source.folders[1]"),
        (["Café", "Cafe\u0301"], "source.folders[1]"),
    ],
)
def test_invalid_folders(tmp_path: Path, folders: object, key: str) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL.replace('["Projects", "Photos"]', json.dumps(folders, ensure_ascii=False)),
        key,
    )


@pytest.mark.parametrize("folder_id", ["short-id", "abcdefghij!", "abcdefghij ", 1234567890])
def test_invalid_folder_id(tmp_path: Path, folder_id: str | int) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL.replace(f'"{_FOLDER_ID}"', json.dumps(folder_id)), "target.folder_id"
    )


@pytest.mark.parametrize("client_id", ["", "example.com", f"{_CLIENT_ID}.invalid", 1])
def test_invalid_client_id(tmp_path: Path, client_id: str | int) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL.replace(f'"{_CLIENT_ID}"', json.dumps(client_id)), "oauth.client_id"
    )


@pytest.mark.parametrize("secret", ["", False])
def test_invalid_client_secret(tmp_path: Path, secret: str | bool) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL.replace('"fixture-secret"', json.dumps(secret)),
        "oauth.client_secret",
    )


@pytest.mark.parametrize(
    "hour", ["24:00", "23:60", "9:00", "09:0", "09:00\n", "\u0660\u0669:\u0660\u0660", "", 2000]
)
def test_invalid_daily_at(tmp_path: Path, hour: str | int) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL + f"\n[schedule]\ndaily_at = {json.dumps(hour)}", "schedule.daily_at"
    )


@pytest.mark.parametrize("value", ["-1", "true", "4.0"])
def test_invalid_max_deletes(tmp_path: Path, value: str) -> None:
    _assert_invalid(tmp_path, _MINIMAL + f"\n[safety]\nmax_deletes = {value}", "safety.max_deletes")


@pytest.mark.parametrize("value", ["0", "-0.1", "1.01", "true", "nan", "+inf", "-inf"])
def test_invalid_max_delete_ratio(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL + f"\n[safety]\nmax_delete_ratio = {value}", "safety.max_delete_ratio"
    )


@pytest.mark.parametrize("value", ["-1", "true", "20.0"])
def test_invalid_ratio_min_files(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path, _MINIMAL + f"\n[safety]\nratio_min_files = {value}", "safety.ratio_min_files"
    )


@pytest.mark.parametrize("value", ["0", "17", "true", "4.0"])
def test_invalid_parallel_uploads(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL + f"\n[transfer]\nparallel_uploads = {value}",
        "transfer.parallel_uploads",
    )


@pytest.mark.parametrize("value", ["0", "257", "true", "32.0"])
def test_invalid_chunk_mib(tmp_path: Path, value: str) -> None:
    _assert_invalid(tmp_path, _MINIMAL + f"\n[transfer]\nchunk_mib = {value}", "transfer.chunk_mib")


@pytest.mark.parametrize("value", ["0", "5", "true", "4.0"])
def test_invalid_multipart_max_mib(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL + f"\n[transfer]\nchunk_mib = 4\nmultipart_max_mib = {value}",
        "transfer.multipart_max_mib",
    )


@pytest.mark.parametrize("value", ["0.09", "20.01", "true", "nan", "inf"])
def test_invalid_writes_per_second(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL + f"\n[transfer]\nwrites_per_second = {value}",
        "transfer.writes_per_second",
    )


@pytest.mark.parametrize("value", ["0.09", "50.01", "true", "nan", "inf"])
def test_invalid_reads_per_second(tmp_path: Path, value: str) -> None:
    _assert_invalid(
        tmp_path,
        _MINIMAL + f"\n[transfer]\nreads_per_second = {value}",
        "transfer.reads_per_second",
    )


@pytest.mark.parametrize(
    ("patterns", "key"),
    [
        ('"*.tmp"', "exclude"),
        ('["*.tmp", ""]', "exclude[1]"),
        ('["*.tmp", "!keep"]', "exclude[1]"),
        ('["*.tmp", "# comment"]', "exclude[1]"),
        ('["*.tmp", 3]', "exclude[1]"),
    ],
)
def test_invalid_exclude(tmp_path: Path, patterns: str, key: str) -> None:
    _assert_invalid(tmp_path, f"exclude = {patterns}\n" + _MINIMAL, key)


@pytest.mark.parametrize("language", ['"fr"', '"en-US"', '""', '"ES"', "false"])
def test_invalid_language(tmp_path: Path, language: str) -> None:
    _assert_invalid(tmp_path, f"language = {language}\n" + _MINIMAL, "language")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("exclude_ignore_case = 1\n" + _MINIMAL, "exclude_ignore_case"),
        ("service = false\n" + _MINIMAL, "service"),
        (_MINIMAL.replace("[source]", '[source]\nrequire_mount = "false"'), "source.require_mount"),
        (_MINIMAL.replace("[target]", "[target]\nlabel = 1"), "target.label"),
        ("source = 9\n" + _MINIMAL[_MINIMAL.index("[target]") :], "source"),
        (_MINIMAL.replace('root = "/media/alice/DATA"\n', ""), "source.root"),
        (_MINIMAL.replace('folders = ["Projects", "Photos"]\n', ""), "source.folders"),
    ],
)
def test_invalid_field_types_and_missing_keys(tmp_path: Path, text: str, key: str) -> None:
    _assert_invalid(tmp_path, text, key)


def test_invalid_toml_and_unreadable_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_bytes(b"schema = \xff")
    with pytest.raises(ConfigError):
        load_config(path)
    with pytest.raises(ConfigError):
        _load(tmp_path, "[source\n")
    with pytest.raises(ConfigError):
        _load(tmp_path, f"schema = {'9' * 5000}\n" + _MINIMAL)


@pytest.mark.parametrize("paused", ['["Unknown"]', '["Projects", "Unknown"]', "[1]"])
def test_paused_must_be_subset(tmp_path: Path, paused: str) -> None:
    parsed = tomllib.loads("paused = " + paused)["paused"]
    index = 1 if len(parsed) > 1 else 0
    _assert_invalid(
        tmp_path,
        _MINIMAL.replace("[source]", f"[source]\npaused = {paused}"),
        f"source.paused[{index}]",
    )


def test_active_folders_order(tmp_path: Path) -> None:
    text = _MINIMAL.replace('["Projects", "Photos"]', '["Projects", "Café", "Notes", "Photos"]')
    cfg = _load(tmp_path, text.replace("[source]", '[source]\npaused = ["Photos", "Café"]'))
    assert cfg.source.folders == ("Projects", "Café", "Notes", "Photos")
    assert cfg.source.paused == ("Photos", "Café")
    assert cfg.active_folders == ("Projects", "Notes")
    assert replace(cfg, source=replace(cfg.source, paused=cfg.source.folders)).active_folders == ()


def test_explicit_excludes_and_case_setting(tmp_path: Path) -> None:
    cfg = _load(tmp_path, 'exclude = ["Notes/"]\nexclude_ignore_case = false\n' + _MINIMAL)
    matcher = cfg.matcher()
    assert matcher.excluded("Notes/a", False)
    assert not matcher.excluded("notes/a", False)
    assert not matcher.excluded("node_modules/a", False)
    assert matcher.excluded("notes/a.nimbo-part", False)


def test_config_accepts_numeric_bounds(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        _MINIMAL
        + """
[safety]
max_deletes = 0
max_delete_ratio = 1
ratio_min_files = 0
[transfer]
parallel_uploads = 1
chunk_mib = 1
multipart_max_mib = 1
writes_per_second = 20
reads_per_second = 0.1
""",
    )
    assert cfg.safety == SafetyConfig(0, 1.0, 0)
    assert cfg.transfer == TransferConfig(1, 1, 1, 20.0, 0.1)


def test_save_is_atomic_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings" / "config.toml"
    save_config(_DEFAULTS, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path) == _DEFAULTS

    changed = replace(_DEFAULTS, language="es")
    original_replace = Path.replace

    def observe_replace(staged: Path, destination: Path) -> Path:
        assert staged != destination == path
        assert staged.parent == destination.parent
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        assert load_config(staged) == changed
        assert load_config(destination) == _DEFAULTS
        return original_replace(staged, destination)

    path.chmod(0o644)
    monkeypatch.setattr(Path, "replace", observe_replace)
    save_config(changed, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path) == changed
    assert list(path.parent.iterdir()) == [path]


def test_save_keeps_previous_config_if_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    save_config(_DEFAULTS, path)

    def fail_replace(staged: Path, destination: Path) -> Path:
        assert staged != destination == path
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(replace(_DEFAULTS, language="es"), path)
    assert load_config(path) == _DEFAULTS
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "text",
    [
        _FOLDER_ID,
        f" {_FOLDER_ID}\n",
        "0123456789",
        f"https://drive.google.com/drive/folders/{_FOLDER_ID}",
        f"https://drive.google.com/drive/u/0/folders/{_FOLDER_ID}?usp=sharing",
        f"https://drive.google.com/open?id={_FOLDER_ID}",
        f"https://drive.google.com/open?usp=sharing&id={_FOLDER_ID}",
    ],
)
def test_parse_folder_id_variants(text: str) -> None:
    assert parse_folder_id(text) == ("0123456789" if text == "0123456789" else _FOLDER_ID)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "abcdefghij!",
        "abc\ndefghij",
        "ébcdefghij",
        f"/drive/folders/{_FOLDER_ID}",
        f"http://drive.google.com/drive/folders/{_FOLDER_ID}",
        f"https://drive.google.com.invalid/drive/folders/{_FOLDER_ID}",
        f"https://evil@drive.google.com/drive/folders/{_FOLDER_ID}",
        f"https://drive.google.com@invalid.example/drive/folders/{_FOLDER_ID}",
        f"https://drive.google.com/drive/fold\ners/{_FOLDER_ID}",
        f"https://drive.google.com/file/d/{_FOLDER_ID}",
        f"https://drive.google.com/drive/folders/{_FOLDER_ID}/extra",
        f"https://drive.google.com/open#id={_FOLDER_ID}",
        "https://drive.google.com/open?id=",
        f"https://drive.google.com/open?id={_FOLDER_ID}&id=other-valid-id",
        f"https://drive.google.com/open?id={_FOLDER_ID}&id=",
        "https://[drive.google.com",
    ],
)
def test_parse_folder_id_rejects_garbage(text: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_folder_id(text)
    assert "folder_id" in exc.value.detail
