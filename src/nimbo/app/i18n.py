"""Load and resolve the application's bilingual message catalogs."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from functools import cache
from importlib.resources import files
from string import Formatter


def detect_language(configured: str | None, env: Mapping[str, str] | None = None) -> str:
    """Return es or en from an explicit setting or the first nonempty locale variable."""
    env = os.environ if env is None else env
    locale = configured or env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    return "es" if locale.lower().startswith("es") else "en"


def _merge(target: dict[str, object], extra: Mapping[str, object], where: str) -> None:
    """Deep-merge ``extra`` into ``target``; a key defined twice is a packaging bug."""
    for key, value in extra.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value, f"{where}.{key}")
        elif key in target:
            raise ValueError(f"duplicate catalog key {where}.{key}")
        else:
            target[key] = value


@cache
def _catalog(language: str) -> dict[str, object]:
    """Load ``<language>.toml`` merged with every ``<language>.<part>.toml`` (sorted).

    Each area of the app (CLI, dashboard, wizard...) can own its own catalog file.
    """
    folder = files("nimbo.app").joinpath("locales")
    catalog: dict[str, object] = {}
    names = sorted(
        entry.name
        for entry in folder.iterdir()
        if entry.name == f"{language}.toml"
        or (entry.name.startswith(f"{language}.") and entry.name.endswith(".toml"))
    )
    for name in names:
        with folder.joinpath(name).open("rb") as handle:
            _merge(catalog, tomllib.load(handle), name)
    return catalog


def _lookup(catalog: dict[str, object], key: str) -> str | None:
    current: object = catalog
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, str) else None


def _format(template: str, values: dict[str, object]) -> str:
    try:
        return template.format_map(values)
    except KeyError:
        # Format the available fields without losing missing fields' format specs.
        parts = []
        for literal, name, spec, conversion in Formatter().parse(template):
            parts.append(literal)
            if name is not None:
                field = "{" + name
                if conversion:
                    field += f"!{conversion}"
                if spec:
                    field += f":{spec}"
                field += "}"
                with suppress(KeyError):
                    field = field.format_map(values)
                parts.append(field)
        return "".join(parts)


class Translator:
    """Resolve dotted keys in the selected catalog, falling back to English and the key."""

    def __init__(self, language: str) -> None:
        """Select a language and reuse the parsed catalogs across instances."""
        self.language = detect_language(language, {})
        self._local = _catalog(self.language)
        self._english = _catalog("en")

    def __call__(self, key: str, /, **kwargs: object) -> str:
        """Translate a key, interpolating named fields and preserving missing placeholders."""
        template = self._message(key)
        return key if template is None else _format(template, kwargs)

    def plural(self, key: str, count: int, /, **kwargs: object) -> str:
        """Translate the .one form for a count of one or .other, supplying count to it."""
        form = "one" if count == 1 else "other"
        kwargs["count"] = count
        return self(f"{key}.{form}", **kwargs)

    def has(self, key: str) -> bool:
        """Return whether a text exists in either catalog for the dotted key."""
        return self._message(key) is not None

    def _message(self, key: str) -> str | None:
        message = _lookup(self._local, key)
        return _lookup(self._english, key) if message is None else message
