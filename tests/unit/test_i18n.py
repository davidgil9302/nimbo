from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from string import Formatter

import pytest

from nimbo.app import i18n
from nimbo.app.i18n import Translator, detect_language
from nimbo.core import errors
from nimbo.core.model import PROGRESS_PHASES, REASONS, RunResult

_SRC = Path(__file__).resolve().parents[2] / "src" / "nimbo"


def _leaves(table: dict, prefix: str = "") -> dict[str, str]:
    leaves = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            leaves.update(_leaves(value, path + "."))
        else:
            assert isinstance(value, str) and value
            leaves[path] = value
    return leaves


def _catalog(language: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    folder = _SRC / "app" / "locales"
    for path in sorted(folder.glob(f"{language}.toml")) + sorted(folder.glob(f"{language}.*.toml")):
        leaves = _leaves(tomllib.loads(path.read_text(encoding="utf-8")))
        assert not merged.keys() & leaves.keys(), f"duplicate keys in {path.name}"
        merged.update(leaves)
    return merged


def test_part_catalogs_are_merged_and_duplicates_rejected(tmp_path, monkeypatch):
    """Area catalogs (<lang>.<part>.toml) merge into the base one; duplicates are a bug."""
    from nimbo.app import i18n

    base = {"common": {"ok": "listo"}}
    i18n._merge(base, {"cli": {"run": "respaldar"}}, "es.cli.toml")
    assert base == {"common": {"ok": "listo"}, "cli": {"run": "respaldar"}}
    with pytest.raises(ValueError, match="duplicate catalog key"):
        i18n._merge(base, {"common": {"ok": "otro"}}, "es.tui.toml")


@pytest.mark.parametrize(
    ("configured", "env", "expected"),
    [
        ("es", {"LC_ALL": "en_US"}, "es"),
        ("en", {"LC_ALL": "es_MX"}, "en"),
        (None, {"LC_ALL": "es_MX.UTF-8", "LANG": "en_US"}, "es"),
        (None, {"LC_ALL": "C", "LC_MESSAGES": "es", "LANG": "es"}, "en"),
        (None, {"LC_ALL": "", "LC_MESSAGES": "ES_ES@modern", "LANG": "en"}, "es"),
        (None, {"LANG": "es-419"}, "es"),
        (None, {"LANG": "pt_BR"}, "en"),
        (None, {}, "en"),
    ],
)
def test_detect_language_matrix(configured, env, expected):
    assert detect_language(configured, env) == expected


def test_fallback_chain(monkeypatch):
    source = {
        "es": {"common": {"hello": "Hola, {name}"}},
        "en": {
            "common": {
                "hello": "Hello",
                "extra": "Try {command!r} in {delay:02d} s",
                "nested": "{{Score}} {score:{width}d}",
            }
        },
    }
    monkeypatch.setattr(i18n, "_catalog", source.__getitem__)
    t = Translator("es")
    assert t("common.hello", name="Alex") == "Hola, Alex"
    assert t("common.hello") == "Hola, {name}"
    assert t("common.extra", command="backup") == "Try 'backup' in {delay:02d} s"
    assert t("common.extra", delay=5) == "Try {command!r} in 05 s"
    assert t("common.nested", score=4) == "{Score} {score:{width}d}"
    assert t("common.nested", score=4, width=3) == "{Score}   4"
    assert t("common.missing") == "common.missing"
    assert t.has("common.extra")
    assert not t.has("common.missing")
    assert not t.has("common")
    assert not t.has("common.hello.invalid")


def test_catalogs_are_cached():
    assert i18n._catalog("es") is i18n._catalog("es")


@pytest.mark.parametrize(
    ("language", "count", "expected"),
    [("es", 1, "1 archivo subido"), ("es", 0, "0 archivos subidos"), ("en", 2, "2 files uploaded")],
)
def test_plural(language, count, expected):
    assert Translator(language).plural("notify.uploaded", count) == expected


def test_catalogs_have_identical_keys():
    es, en = _catalog("es"), _catalog("en")
    assert es.keys() == en.keys()
    formatter = Formatter()
    for key in es:
        assert sorted(field[1:] for field in formatter.parse(es[key]) if field[1]) == sorted(
            field[1:] for field in formatter.parse(en[key]) if field[1]
        ), key


def test_every_key_used_in_code_exists():
    keys = _catalog("es")
    references = re.compile(r"\bt(?P<plural>\.plural)?\(\s*[\"\'](?P<key>[\w.-]+)[\"\']")
    missing = set()
    for path in _SRC.rglob("*.py"):
        for match in references.finditer(path.read_text(encoding="utf-8")):
            key = match["key"]
            needed = {f"{key}.one", f"{key}.other"} if match["plural"] else {key}
            missing.update(needed - keys.keys())
    assert not missing


def test_every_reason_code_has_text():
    t = Translator("es")
    reason_codes = {
        value.code
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, errors.NimboError)
    }
    reason_codes.update(REASONS)
    reason_codes.update(RunResult)
    for name in ("safety", "scanner", "planner", "executor", "engine", "remote_sync", "index"):
        tree = ast.parse((_SRC / "core" / f"{name}.py").read_text(encoding="utf-8"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            literals = [kw.value for kw in call.keywords if kw.arg == "code"]
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in {"Problem", "PlanIssue", "ScanProblem", "_PreflightError"}
                and call.args
            ):
                literals.append(call.args[0])
            reason_codes.update(
                node.value
                for node in literals
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            )
    reason_codes.update(
        [
            "reauth_required",
            "auth_failed",
            "auth_denied",
            "auth_timeout",
            "auth_scope_missing",
            "auth_network",
            "secret_store_locked",
            "secret_store_unavailable",
            "internal_error",
            "plan_inconsistent",
            "foreign_owner",
            "file_in_use",
            "move_rejected",
            "local_vanished",
            "local_unreadable",
            "remote_changed",
            "remote_missing",
            "changed_since_trash",
            "folder-kept-protected",
            "trash-took-new-items",
            "folder_ratio",
            "too_many_deletes",
        ]
    )
    assert {code for code in reason_codes if not t.has(f"reason.{code}")} == set()
    assert all(t.has(f"result.{result}") for result in RunResult)
    assert all(t.has(f"phase.{phase}") for phase in PROGRESS_PHASES)


@pytest.mark.parametrize("language", ["es", "en"])
def test_no_all_caps_labels(language):
    for key, value in _catalog(language).items():
        words = re.findall(r"\b\w+\b", value)
        allowed = {"MD5", "API", "URL", "OAuth", "Drive", "Google"}
        assert not {word for word in words if len(word) > 3 and word.isupper()} - allowed, key
        if key.startswith(("reason.", "result.")) or key.endswith(".title"):
            assert value[0].isupper(), key
            assert not re.search(r"[!¡]", value), key
