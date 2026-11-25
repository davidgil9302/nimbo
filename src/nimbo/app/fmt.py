"""Format sizes, elapsed times, timestamps and progress for people."""

from __future__ import annotations

import math
from datetime import datetime

from .i18n import Translator

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def human_size(n: int | None, lang: str) -> str:
    """Return a binary-scaled size with a localized decimal separator or an em dash."""
    t = Translator(lang)
    if n is None:
        return t("common.unknown")
    n = max(n, 0)
    if n < 1024:
        return f"{n} B"
    power = min((n.bit_length() - 1) // 10, len(_SIZE_UNITS) - 1)
    scale = 1024**power
    whole, decimal = divmod((10 * n + scale // 2) // scale, 10)
    separator = t("units.decimal_separator")
    return f"{whole}{separator}{decimal} {_SIZE_UNITS[power]}"


def human_duration(seconds: float | None, lang: str) -> str:
    """Return up to two duration units or an em dash for an unavailable duration."""
    t = Translator(lang)
    if seconds is None or not math.isfinite(seconds):
        return t("common.unknown")
    minutes, secs = divmod(max(int(seconds), 0), 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)
    if days:
        return t("units.days_hours", days=days, hours=hrs)
    if hours:
        return t("units.hours_minutes", hours=hours, minutes=mins)
    if minutes:
        return t("units.minutes_seconds", minutes=minutes, seconds=secs)
    return t("units.seconds", seconds=secs)


def relative_time(moment: datetime | None, now: datetime, t: Translator) -> str:
    """Format a timestamp using now's calendar and timezone, if both times are aware."""
    if moment is None:
        return t("common.unknown")
    if moment.utcoffset() is not None and now.utcoffset() is not None:
        moment = moment.astimezone(now.tzinfo)
    day_delta = (now.date() - moment.date()).days
    time = f"{moment.hour:02d}:{moment.minute:02d}"
    if day_delta == 0:
        return t("time.today", time=time)
    if day_delta == 1:
        return t("time.yesterday", time=time)
    month = t(f"time.month.{moment.month}")
    if moment.year == now.year:
        return t("time.same_year", day=moment.day, month=month, time=time)
    return t("time.other_year", day=moment.day, month=month, year=moment.year, time=time)


def percent(done: int, total: int) -> int:
    """Floor progress to a whole percent and clamp it to 0-100.

    A nonpositive total with nonnegative progress counts as complete, including
    0 out of 0, which means there is nothing to do.
    """
    if total <= 0:
        return 100 if done >= 0 else 0
    return min(max(100 * done // total, 0), 100)
