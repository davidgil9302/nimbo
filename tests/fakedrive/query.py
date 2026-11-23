"""Parser for the supported Drive files.list query expressions."""

from __future__ import annotations

import re
from collections.abc import Callable

from .state import FakeFile

_Predicate = Callable[[FakeFile], bool]
_TOKEN = re.compile(
    r"\s*(?:(?P<literal>'(?:[^'\\]|\\['\\])*')|(?P<word>[A-Za-z]+)|(?P<operator>!=|[()=]))"
)
_INVALID = "Invalid query"


class _Parser:
    def __init__(self, query: str, resolve_id: Callable[[str], str]) -> None:
        self.tokens: list[str] = []
        self.pos = 0
        self.resolve_id = resolve_id
        query = query.strip()
        pos = 0
        while pos < len(query):
            match = _TOKEN.match(query, pos)
            if match is None:
                raise ValueError(_INVALID)
            self.tokens.append(match[1] or match[2] or match[3])
            pos = match.end()

    def take(self, *choices: str) -> str | None:
        if self.pos < len(self.tokens) and self.tokens[self.pos] in choices:
            self.pos += 1
            return self.tokens[self.pos - 1]
        return None

    def expect(self, *choices: str) -> str:
        value = self.take(*choices)
        if value is None:
            raise ValueError(_INVALID)
        return value

    def literal(self) -> str:
        if self.pos == len(self.tokens) or not self.tokens[self.pos].startswith("'"):
            raise ValueError(_INVALID)
        self.pos += 1
        return re.sub(r"\\(.)", r"\1", self.tokens[self.pos - 1][1:-1])

    def expression(self, depth: int = 0) -> tuple[_Predicate, bool]:
        terms = [self.parent_group(depth)]
        while self.take("and"):
            terms.append(self.parent_group(depth))
        if len(terms) == 1:
            return terms[0]
        return lambda item: all(test(item) for test, _ in terms), False

    def parent_group(self, depth: int) -> tuple[_Predicate, bool]:
        terms = [self.term(depth)]
        while self.take("or"):
            terms.append(self.term(depth))
        if len(terms) == 1:
            return terms[0]
        if not all(is_parent for _, is_parent in terms):
            raise ValueError(_INVALID)
        return lambda item: any(test(item) for test, _ in terms), True

    def term(self, depth: int) -> tuple[_Predicate, bool]:
        if depth > 20:
            raise ValueError(_INVALID)
        if self.take("("):
            nested = self.expression(depth + 1)
            self.expect(")")
            return nested
        key = self.take("trashed", "name", "mimeType")
        if key is None:
            parent = self.resolve_id(self.literal())
            self.expect("in")
            self.expect("parents")
            return lambda item: parent in item.parents, True
        operator = self.expect("=", "!=") if key == "mimeType" else self.expect("=")
        if key == "trashed":
            trashed = self.expect("true", "false") == "true"
            return lambda item: item.trashed == trashed, False
        value = self.literal()
        if key == "name":
            return lambda item: item.name == value, False
        return lambda item: (item.mime_type == value) == (operator == "="), False


def predicate(query: str | None, resolve_id: Callable[[str], str]) -> _Predicate:
    """Compile a supported files.list query, raising ValueError for invalid syntax."""
    if query is None:
        return lambda _: True
    parser = _Parser(query, resolve_id)
    test, _ = parser.expression()
    if parser.pos != len(parser.tokens):
        raise ValueError(_INVALID)
    return test
