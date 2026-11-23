"""Tests for the Git-style exclusion rules used by local and remote scans."""

import pytest

from nimbo.core.errors import ConfigError
from nimbo.core.filters import BUILTIN_EXCLUDES, Matcher, PatternError
from nimbo.core.model import PART_SUFFIX, SENTINEL_NAME


def test_basename_pattern_matches_any_depth() -> None:
    matcher = Matcher(["*.bin"])
    assert matcher.excluded("a.bin", False)
    assert matcher.excluded("a/b/c.bin", False)
    assert not matcher.excluded("a/b/c.bin.txt", False)


def test_anchored_pattern() -> None:
    matcher = Matcher(["Projects/old"])
    assert matcher.excluded("Projects/old", True)
    assert not matcher.excluded("Archive/Projects/old", True)


def test_leading_slash_anchors() -> None:
    matcher = Matcher(["/cache/"])
    assert matcher.excluded("cache/a", False)
    assert not matcher.excluded("Projects/cache/a", False)


def test_dir_only_pattern_ignores_files() -> None:
    matcher = Matcher(["cache/"])
    assert matcher.excluded("Projects/cache", True)
    assert not matcher.excluded("Projects/cache", False)


def test_double_star_prefix_middle_suffix() -> None:
    prefix = Matcher(["**/Projects/old"])
    middle = Matcher(["Projects/**/old"])
    suffix = Matcher(["Projects/**"])
    assert prefix.excluded("Projects/old", False)
    assert prefix.excluded("a/b/Projects/old", False)
    assert middle.excluded("Projects/old", False)
    assert middle.excluded("Projects/a/b/old", False)
    assert not middle.excluded("Other/Projects/a/old", False)
    assert suffix.excluded("Projects/old", False)
    assert suffix.excluded("Projects/a/b", True)
    assert not suffix.excluded("Projects", True)


def test_char_classes_and_negated_classes() -> None:
    assert Matcher(["file[0-3].txt"]).excluded("file2.txt", False)
    assert not Matcher(["file[0-3].txt"]).excluded("file8.txt", False)
    assert Matcher(["file[!0-3].txt"]).excluded("file8.txt", False)
    assert not Matcher(["file[!0-3].txt"]).excluded("file2.txt", False)


def test_regex_metachars_are_literal() -> None:
    matcher = Matcher(["a+b(1).txt", "$RECYCLE.BIN/"])
    assert matcher.excluded("Projects/a+b(1).txt", False)
    assert matcher.excluded("$RECYCLE.BIN/a", False)
    assert not matcher.excluded("ab1.txt", False)
    assert not matcher.excluded("$RECYCLEaBIN/a", False)


def test_ancestor_exclusion_applies_to_descendants() -> None:
    matcher = Matcher(["/Projects/old/"])
    for rel in ("Projects/old/a", "Projects/old/a/b", "Projects/old/b"):
        assert matcher.excluded(rel, False)
        assert matcher.explain(rel, False) == "/Projects/old/"
    assert not matcher.excluded("Projects/older/a", False)
    assert not matcher.excluded("Projects/old", False)


def test_ignore_case_default_and_off() -> None:
    assert Matcher(["É[AB]/"]).excluded("éa/a", False)
    assert not Matcher(["É[AB]/"], ignore_case=False).excluded("éa/a", False)
    assert Matcher(["É[AB]/"], ignore_case=False).excluded("ÉA/a", False)


def test_builtin_excludes_always_on() -> None:
    matcher = Matcher([])
    assert (SENTINEL_NAME, f"*{PART_SUFFIX}") == BUILTIN_EXCLUDES
    assert matcher.explain(SENTINEL_NAME, False) == SENTINEL_NAME
    assert matcher.explain(f"Projects/file{PART_SUFFIX}", False) == f"*{PART_SUFFIX}"
    assert not matcher.excluded(f"file{PART_SUFFIX}.txt", False)


@pytest.mark.parametrize("pattern", ["", "!secret", "# comment", "!", "#"])
def test_negation_and_comments_rejected(pattern: str) -> None:
    with pytest.raises(PatternError) as exc:
        Matcher(["valid", pattern])
    assert isinstance(exc.value, ConfigError)
    assert exc.value.code == "exclude_pattern_invalid"


def test_explain_returns_pattern() -> None:
    matcher = Matcher(["*.bin", "cache/"])
    assert matcher.explain("Projects/cache/content", False) == "cache/"
    assert matcher.explain("Projects/a.bin", False) == "*.bin"
    assert matcher.explain("Projects/a.txt", False) is None


def test_nfc_normalization() -> None:
    nfd_pattern = "Cafe\u0301/"
    assert Matcher([nfd_pattern]).excluded("Café/notes", False)
    assert Matcher(["Café/"]).excluded("Cafe\u0301/notes", False)
    assert Matcher([nfd_pattern]).explain("Café/notes", False) == nfd_pattern


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        ("temp", "temp", False, True),
        ("temp", "Projects/temp", True, True),
        ("temp", "Projects/temp/a", False, True),
        ("temp", "Projects/temperature/a", False, False),
        ("temp/", "Projects/temp", False, False),
        ("temp/", "Projects/temp", True, True),
        ("temp/", "temp/a", False, True),
        ("temp/", "tempfile/a", False, False),
        ("/temp", "temp", False, True),
        ("/temp", "Projects/temp", False, False),
        ("/temp/", "temp", False, False),
        ("/temp/", "temp/a", False, True),
        ("Projects/temp", "Projects/temp", False, True),
        ("Projects/temp", "Projects/temp/a", False, True),
        ("Projects/temp", "Local/Projects/temp", False, False),
        ("Projects/temp/", "Projects/temp", False, False),
        ("Projects/temp/", "Projects/temp", True, True),
        ("Projects/*/cache", "Projects/a/cache", False, True),
        ("Projects/*/cache", "Projects/a/b/cache", False, False),
        ("Projects/?/cache", "Projects/é/cache", True, True),
        ("Projects/?/cache", "Projects/ab/cache", True, False),
        ("**/cache", "cache", False, True),
        ("**/cache", "a/b/cache", False, True),
        ("**/cache/", "a/cache", False, False),
        ("**/cache/", "a/cache/x", False, True),
        ("**/a/cache", "x/y/a/cache", False, True),
        ("**/a/cache", "x/a/b/cache", False, False),
        ("Projects/**/cache", "Projects/cache", False, True),
        ("Projects/**/cache", "Projects/a/b/cache", True, True),
        ("Projects/**/cache", "x/Projects/a/cache", True, False),
        ("Projects/**/cache", "Projects/a/caches", True, False),
        ("Projects/**", "Projects", True, False),
        ("Projects/**", "Projects/a", False, True),
        ("Projects/**", "Projects/a/b", True, True),
        ("Projects/**", "x/Projects/a", False, False),
        ("Projects/**/", "Projects/a", False, False),
        ("Projects/**/", "Projects/a", True, True),
        ("Projects/**/", "Projects/a/b", False, True),
        ("a/**/**/b", "a/b", False, True),
        ("a/**/**/b", "a/x/y/b", False, True),
        ("a/**x/b", "a/prefixx/b", False, True),
        ("a/**x/b", "a/x/yx/b", False, False),
        ("[0-9][!x].txt", "a/3a.txt", False, True),
        ("[0-9][!x].txt", "a/3x.txt", False, False),
        ("file[?*].txt", "a/file*.txt", False, True),
        ("file[].].txt", "a/file].txt", False, True),
        ("[[]abc].txt", "[abc].txt", False, True),
        ("[a/]/file", "a/file", False, True),
        ("[a/]/file", "x/a/file", False, False),
        ("[a/]/file", "[a/]/file", False, False),
        ("look\\here", r"a/look\here", False, True),
        ("a+b(1).txt", "a/ab1.txt", False, False),
        ("*.tmp", ".hidden.tmp", False, True),
        ("a?b", "a\nb", False, True),
        ("*", "", True, False),
        ("**/", "x", False, False),
        ("**/", "x/y", False, True),
    ],
)
def test_matches_gitignore_reference_table(
    pattern: str, path: str, is_dir: bool, expected: bool
) -> None:
    assert Matcher([pattern]).excluded(path, is_dir) is expected
