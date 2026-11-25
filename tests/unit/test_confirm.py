"""Localized confirmation input and prompt rendering."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from nimbo.app import cli
from nimbo.app.i18n import Translator
from nimbo.app.wizard import confirm


@pytest.mark.parametrize(
    ("language", "answer", "expected"),
    [
        ("es", "s", True),
        ("es", "S", True),
        ("es", "si", True),
        ("es", "sí", True),
        ("es", "Sí", True),
        ("es", "SÍ", True),
        ("es", " sí ", True),
        ("es", "si\u0301", True),
        ("es", "n", False),
        ("es", "N", False),
        ("es", "no", False),
        ("es", "NO", False),
        ("es", "", False),
        ("en", "y", True),
        ("en", "yes", True),
        ("en", "YES", True),
        ("en", "n", False),
        ("en", "no", False),
        ("en", "", False),
    ],
)
def test_confirm_accepts_localized_answers(language, answer, expected):
    console = Console(file=StringIO())
    stream = StringIO(f"{answer}\n")
    assert confirm(Translator(language), console, "Confirm?", stream=stream) is expected


def test_confirm_defaults_to_no_at_stream_eof():
    output = StringIO()
    console = Console(file=output, width=80)
    assert confirm(Translator("es"), console, "¿Confirmar?", stream=StringIO()) is False
    assert output.getvalue() == "¿Confirmar? [s/N]: "


@pytest.mark.parametrize(
    ("language", "answers", "prompt", "invalid"),
    [
        ("es", "yes\ns\n", "Confirm? [s/N]: ", "Responde «s» para sí o «n» para no."),
        ("en", "sí\ny\n", "Confirm? [y/N]: ", "Answer “y” for yes or “n” for no."),
    ],
)
def test_confirm_retries_with_localized_feedback(language, answers, prompt, invalid):
    output = StringIO()
    console = Console(file=output, width=80)
    assert confirm(Translator(language), console, "Confirm?", stream=StringIO(answers)) is True
    assert output.getvalue() == f"{prompt}{invalid}\n{prompt}"


@pytest.mark.parametrize(("stdin", "expected"), [("SÍ\n", True), ("", False)])
def test_confirm_reads_stdin_and_prints_markup_literally(monkeypatch, stdin, expected):
    monkeypatch.setattr(cli.sys, "stdin", StringIO(stdin))
    output = StringIO()
    console = Console(file=output, width=100)
    message = "¿Usar la cuenta de Google [bold]x[/bold] para este respaldo?"
    assert confirm(Translator("es"), console, message) is expected
    assert output.getvalue() == f"{message} [s/N]: "
