"""Exclusion matching for NFC paths relative to the backup root."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import translate
from functools import lru_cache

from nimbo.core.errors import ConfigError
from nimbo.core.model import PART_SUFFIX, SENTINEL_NAME, nfc, rel_parent

BUILTIN_EXCLUDES: tuple[str, ...] = (SENTINEL_NAME, "*" + PART_SUFFIX)

# Only slashes outside character classes separate components.
_COMPONENT_SEPARATOR = re.compile(r"\[!?\]?[^\]]*\]|(/)")
_ANY_COMPONENT = re.compile(translate("*"))


class PatternError(ConfigError):
    """Report an invalid exclusion rule with its zero-based index in the pattern list."""

    code = "exclude_pattern_invalid"


@dataclass(frozen=True, slots=True)
class _Pattern:
    text: str
    dir_only: bool
    basename_only: bool
    parts: tuple[re.Pattern[str] | None, ...]

    def matches(self, components: list[str], is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.basename_only:
            pattern = self.parts[0]
            assert pattern is not None
            return pattern.fullmatch(components[-1]) is not None
        if None not in self.parts:
            return len(components) == len(self.parts) and all(
                part is not None and part.fullmatch(name) is not None
                for part, name in zip(self.parts, components, strict=True)
            )

        # positions[i] means the pattern so far matched the first i components.
        positions = [True, *([False] * len(components))]
        for part in self.parts:
            if part is None:
                # A globstar can advance from any reachable position to the end.
                first = positions.index(True)
                positions[first:] = [True] * (len(positions) - first)
            else:
                positions = [
                    False,
                    *(
                        reachable and part.fullmatch(name) is not None
                        for reachable, name in zip(positions[:-1], components, strict=True)
                    ),
                ]
                if not any(positions):
                    return False
        return positions[-1]


def _compile(text: str, flags: re.RegexFlag) -> _Pattern:
    normalized = nfc(text)
    dir_only = normalized.endswith("/")
    glob = normalized.removesuffix("/").removeprefix("/")
    basename_only = not normalized.startswith("/") and "/" not in glob
    parts: list[re.Pattern[str] | None] = []
    segments: list[str] = []
    start = 0
    for match in _COMPONENT_SEPARATOR.finditer(glob):
        if match.group(1) is not None:
            segments.append(glob[start : match.start()])
            start = match.end()
    segments.append(glob[start:])

    for index, segment in enumerate(segments):
        if segment == "**" and len(segments) > 1:
            if index == len(segments) - 1:
                # A terminal /** matches inside the directory, starting one level below it.
                parts.append(_ANY_COMPONENT)
            parts.append(None)
        else:
            parts.append(re.compile(translate(segment), flags))
    return _Pattern(text, dir_only, basename_only, tuple(parts))


class Matcher:
    """Compile custom and built-in exclusion globs, optionally ignoring case.

    Empty rules, comments and negations raise PatternError. Built-in rules
    precede custom rules, which retain their supplied order.
    """

    def __init__(self, patterns: Iterable[str], *, ignore_case: bool = True) -> None:
        """Compile the supplied patterns once and create a bounded directory cache."""
        flags = re.IGNORECASE if ignore_case else re.NOFLAG
        compiled = [_compile(pattern, flags) for pattern in BUILTIN_EXCLUDES]
        for index, pattern in enumerate(patterns):
            if not pattern or pattern.startswith(("#", "!")):
                raise PatternError(f"exclude[{index}]")
            compiled.append(_compile(pattern, flags))
        self._patterns = tuple(compiled)
        self._directory_pattern = lru_cache(maxsize=8192)(self._match_directory)

    def _match(self, rel: str, is_dir: bool) -> str | None:
        components = rel.split("/")
        return next(
            (pattern.text for pattern in self._patterns if pattern.matches(components, is_dir)),
            None,
        )

    def _match_directory(self, rel: str) -> str | None:
        return self._match(rel, True)

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """Return whether a relative path or any of its parent directories is excluded."""
        return self.explain(rel, is_dir) is not None

    def explain(self, rel: str, is_dir: bool) -> str | None:
        """Return the first matching rule for the path, then check its nearest parents.

        The rule is returned in its original spelling. The root and paths
        without a matching rule return None.
        """
        rel = nfc(rel)
        if not rel:
            return None
        match = self._directory_pattern(rel) if is_dir else self._match(rel, False)
        while match is None and (rel := rel_parent(rel)):
            match = self._directory_pattern(rel)
        return match
