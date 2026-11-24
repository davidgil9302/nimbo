from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from nimbo.app.fmt import human_duration, human_size, percent, relative_time
from nimbo.app.i18n import Translator


@pytest.mark.parametrize(
    ("size", "es", "en"),
    [
        (None, "—", "—"),
        (-1, "0 B", "0 B"),
        (0, "0 B", "0 B"),
        (512, "512 B", "512 B"),
        (1023, "1023 B", "1023 B"),
        (1024, "1,0 KB", "1.0 KB"),
        (1536, "1,5 KB", "1.5 KB"),
        (1_288_490_188, "1,2 GB", "1.2 GB"),
        (1024**6, "1,0 EB", "1.0 EB"),
    ],
)
def test_human_size_es_en(size, es, en):
    assert human_size(size, "es") == es
    assert human_size(size, "en") == en


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "—"),
        (float("inf"), "—"),
        (float("nan"), "—"),
        (-0.7, "0 s"),
        (45.9, "45 s"),
        (60, "1 min 00 s"),
        (125, "2 min 05 s"),
        (3784, "1 h 03 min"),
        (187220, "2 d 04 h"),
    ],
)
def test_human_duration(seconds, expected):
    assert human_duration(seconds, "es") == expected
    assert human_duration(seconds, "en") == expected


def test_relative_time_today_yesterday_older():
    now = datetime(2026, 11, 25, 23, 40, tzinfo=UTC)
    moments = [
        None,
        datetime(2026, 11, 25, 20, 3, tzinfo=UTC),
        datetime(2026, 11, 24, 20, 1, tzinfo=UTC),
        datetime(2026, 11, 23, 20, 3, tzinfo=UTC),
        datetime(2025, 11, 23, 20, 3, tzinfo=UTC),
    ]
    assert [relative_time(moment, now, Translator("es")) for moment in moments] == [
        "—",
        "hoy, 20:03",
        "ayer, 20:01",
        "23 nov, 20:03",
        "23 nov 2025, 20:03",
    ]
    assert [relative_time(moment, now, Translator("en")) for moment in moments] == [
        "—",
        "today, 20:03",
        "yesterday, 20:01",
        "Nov 23, 20:03",
        "Nov 23, 2025, 20:03",
    ]


def test_relative_time_uses_display_timezone_and_crosses_year_boundary():
    now = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6)))
    moment = datetime(2026, 1, 1, 2, 1, tzinfo=UTC)
    assert relative_time(moment, now, Translator("es")) == "ayer, 20:01"


def test_relative_time_keeps_naive_wall_time():
    now = datetime(2026, 11, 23, 20, 40, tzinfo=UTC)
    moment = datetime(2026, 11, 23, 20, 3)
    assert relative_time(moment, now, Translator("en")) == "today, 20:03"


@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [
        (0, 0, 100),
        (2, 0, 100),
        (-1, 0, 0),
        (-1, 2, 0),
        (0, 2, 0),
        (1, 3, 33),
        (3, 2, 100),
        (2**2048, 2**2049, 50),
    ],
)
def test_percent_edges(done, total, expected):
    assert percent(done, total) == expected
