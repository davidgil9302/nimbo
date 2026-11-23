"""Thread-safe Drive request throttling and bounded retry timing."""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Literal


class TokenBucket:
    """A thread-safe token bucket with a fixed capacity, initially full."""

    def __init__(
        self,
        rate: float,
        burst: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Set the refill rate and capacity, raising ValueError for invalid limits.

        The clock is monotonic and both the clock and sleep use seconds.
        """
        if not (math.isfinite(rate) and rate > 0 and math.isfinite(burst) and burst > 0):
            raise ValueError("rate and burst must be finite and positive")
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = burst
        self._updated = clock()
        self._lock = Lock()

    def _take_or_delay(self, tokens: float) -> float:
        with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            # Even very small leftovers must advance a floating-point clock.
            return max((tokens - self._tokens) / self._rate, math.ulp(now))

    def acquire(self, tokens: float = 1.0) -> None:
        """Consume tokens, waiting outside the lock until enough are available.

        The requested amount must be positive and at most the burst capacity.
        Raise ValueError if this condition fails.
        """
        if not 0 < tokens <= self._burst:
            raise ValueError("tokens must be positive and at most the burst")
        while delay := self._take_or_delay(tokens):
            self._sleep(delay)


class RateLimiter:
    """Independent buckets for reads and writes with a shared penalty deadline."""

    def __init__(
        self,
        *,
        reads_per_second: float,
        writes_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Start full buckets, raising ValueError for nonpositive or nonfinite rates."""
        self._buckets = {
            "read": TokenBucket(
                reads_per_second, max(2 * reads_per_second, 5), clock=clock, sleep=sleep
            ),
            "write": TokenBucket(
                writes_per_second, max(2 * writes_per_second, 3), clock=clock, sleep=sleep
            ),
        }
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()
        self._penalty_until = -math.inf

    def acquire(self, kind: Literal["read", "write"]) -> None:
        """Wait for the shared penalty and consume one read or write token."""
        bucket = self._buckets[kind]
        while True:
            with self._lock:
                delay = self._penalty_until - self._clock()
                if delay <= 0:
                    delay = bucket._take_or_delay(1.0)
                    if delay == 0:
                        return
            self._sleep(delay)

    def penalize(self, seconds: float) -> None:
        """Extend the shared pause to at least now plus seconds.

        Raise ValueError for a nonfinite duration.
        """
        if not math.isfinite(seconds):
            raise ValueError("penalty duration must be finite")
        with self._lock:
            self._penalty_until = max(self._penalty_until, self._clock() + seconds)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Total attempt budget and exponential retry bounds in seconds."""

    max_attempts: int = 8
    base_delay: float = 1.0
    max_delay: float = 64.0

    def delay(self, attempt: int, *, retry_after: float | None = None, rng: random.Random) -> float:
        """Return full-jitter delay with an optional Retry-After floor capped at 300 s.

        The first retry is attempt 1. Earlier attempt numbers raise ValueError.
        """
        if attempt < 1:
            raise ValueError("retry attempt must be at least 1")
        try:
            bound = min(self.max_delay, math.ldexp(self.base_delay, attempt - 1))
        except OverflowError:
            bound = self.max_delay
        jitter = rng.uniform(0, bound)
        return jitter if retry_after is None else max(jitter, min(retry_after, 300.0))


_WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_LONG_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_HTTP_DATE = re.compile(
    rf"{_WEEKDAY}, [0-9]{{2}} {_MONTH} (?P<imf_year>[0-9]{{4}}) {_TIME} GMT"
    rf"|{_LONG_WEEKDAY}, [0-9]{{2}}-{_MONTH}-(?P<short_year>[0-9]{{2}}) {_TIME} GMT"
    rf"|{_WEEKDAY} {_MONTH} (?: [0-9]|[0-9]{{2}}) {_TIME} (?P<asctime_year>[0-9]{{4}})"
)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Return nonnegative seconds from an HTTP Retry-After header.

    Missing or invalid headers return None. Naive reference times are UTC.
    """
    if value is None:
        return None
    value = value.strip(" \t")
    if re.fullmatch(r"[0-9]+", value):
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    match = _HTTP_DATE.fullmatch(value)
    if match is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    year_field = match.lastgroup
    assert year_field is not None
    year = int(match[year_field])
    start, end = match.span(year_field)
    # Parse in a leap year and then set the actual year to avoid the email parser's pivot.
    try:
        moment = parsedate_to_datetime(f"{value[:start]}2000{value[end:]}")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if year_field == "short_year":
            now = now.astimezone(UTC)
            offset = (year - now.year + 49) % 100 - 49
            if offset == 50 and moment > now.replace(year=2000):
                offset = -50
            year = now.year + offset
        return max(0.0, (moment.replace(year=year) - now).total_seconds())
    except (ValueError, OverflowError):
        return None
