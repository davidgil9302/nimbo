"""Tests for token and penalty timing with an injected monotonic clock."""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event, Lock
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nimbo.core.ratelimit import RateLimiter, RetryPolicy, TokenBucket, parse_retry_after


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.lock = Lock()

    def clock(self) -> float:
        """Return simulated monotonic time in seconds."""
        with self.lock:
            return self.now

    def sleep(self, delay: float) -> None:
        """Advance the clock instead of blocking the caller."""
        assert delay > 0
        with self.lock:
            self.sleeps.append(delay)
            self.now += delay


@pytest.mark.parametrize("rate", [0.1, 1.0, 3.0, 4.5, 10.0])
def test_bucket_rate_over_simulated_minute(rate: float) -> None:
    fake = _FakeClock(100_000.0)
    bucket = TokenBucket(rate, 1, clock=fake.clock, sleep=fake.sleep)
    bucket.acquire()
    for _ in range(round(60 * rate)):
        bucket.acquire()
    assert fake.clock() == pytest.approx(100_060.0, abs=1e-7, rel=0)


def test_bucket_burst() -> None:
    fake = _FakeClock()
    bucket = TokenBucket(2.0, 3.0, clock=fake.clock, sleep=fake.sleep)
    bucket.acquire(1.5)
    bucket.acquire(1.5)
    assert not fake.sleeps
    bucket.acquire()
    assert fake.sleeps == [0.5]
    fake.sleep(12)
    bucket.acquire(3)
    assert fake.sleeps == [0.5, 12]
    bucket.acquire(0.5)
    assert fake.sleeps[-1] == 0.25


def test_bucket_thread_safety() -> None:
    fake = _FakeClock()
    bucket = TokenBucket(8, 8, clock=fake.clock, sleep=fake.sleep)
    start = Barrier(8)
    burst = Barrier(8)

    def take_tokens() -> None:
        start.wait(timeout=5)
        bucket.acquire()
        burst.wait(timeout=5)
        for _ in range(10):
            bucket.acquire()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(take_tokens) for _ in range(8)]
        for future in futures:
            future.result(timeout=5)

    assert fake.clock() >= 10.0
    assert all(sleep == 0.125 for sleep in fake.sleeps)


def test_bucket_sleeps_outside_lock() -> None:
    fake = _FakeClock()
    sleeping = Event()
    resume = Event()

    def sleep(delay: float) -> None:
        sleeping.set()
        assert resume.wait(timeout=5)
        fake.sleep(delay)

    bucket = TokenBucket(1, 2, clock=fake.clock, sleep=sleep)
    bucket.acquire()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(bucket.acquire, 2)
        try:
            assert sleeping.wait(timeout=5)
            pool.submit(bucket.acquire).result(timeout=5)
        finally:
            resume.set()
        pending.result(timeout=5)
    assert fake.sleeps == [1.0, 1.0]


@pytest.mark.parametrize("tokens", [0, -1, 3, float("inf"), float("nan")])
def test_bucket_rejects_invalid_requests(tokens: float) -> None:
    fake = _FakeClock()
    bucket = TokenBucket(1, 2, clock=fake.clock, sleep=fake.sleep)
    with pytest.raises(ValueError):
        bucket.acquire(tokens)
    assert not fake.sleeps


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (1, 0), (-1, 1), (1, float("inf"))])
def test_bucket_rejects_invalid_limits(rate: float, burst: float) -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate, burst)


@pytest.mark.parametrize("first", ["read", "write"])
def test_penalty_blocks_all_kinds(first: Literal["read", "write"]) -> None:
    fake = _FakeClock()
    limiter = RateLimiter(
        reads_per_second=10, writes_per_second=3, clock=fake.clock, sleep=fake.sleep
    )
    limiter.penalize(5)
    limiter.acquire(first)
    assert fake.clock() == 5
    limiter.acquire("write" if first == "read" else "read")
    assert fake.sleeps == [5.0]


def test_penalty_max_wins() -> None:
    fake = _FakeClock(20)
    limiter = RateLimiter(
        reads_per_second=10, writes_per_second=3, clock=fake.clock, sleep=fake.sleep
    )
    limiter.penalize(10)
    fake.sleep(2)
    limiter.penalize(5)
    limiter.acquire("read")
    assert fake.clock() == 30
    limiter.penalize(2)
    limiter.penalize(6)
    limiter.acquire("write")
    assert fake.clock() == 36


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_penalty_does_not_change_deadline(seconds: float) -> None:
    fake = _FakeClock()
    limiter = RateLimiter(
        reads_per_second=1, writes_per_second=1, clock=fake.clock, sleep=fake.sleep
    )
    limiter.penalize(3)
    with pytest.raises(ValueError, match="finite"):
        limiter.penalize(seconds)
    limiter.acquire("read")
    assert fake.sleeps == [3.0]


def test_penalty_can_be_extended_during_token_wait() -> None:
    fake = _FakeClock()
    sleeping = Event()
    resume = Event()

    def sleep(delay: float) -> None:
        if not sleeping.is_set():
            sleeping.set()
            assert resume.wait(timeout=5)
        fake.sleep(delay)

    limiter = RateLimiter(reads_per_second=1, writes_per_second=1, clock=fake.clock, sleep=sleep)
    for _ in range(3):
        limiter.acquire("write")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(limiter.acquire, "write")
        try:
            assert sleeping.wait(timeout=5)
            pool.submit(limiter.penalize, 4).result(timeout=5)
        finally:
            resume.set()
        pending.result(timeout=5)
    assert fake.sleeps == [1, 3]


@pytest.mark.parametrize(
    ("kind", "rate", "burst"),
    [("read", 1, 5), ("write", 1, 3), ("read", 10, 20), ("write", 3, 6)],
)
def test_read_and_write_bursts(kind: Literal["read", "write"], rate: float, burst: int) -> None:
    fake = _FakeClock()
    limiter = RateLimiter(
        reads_per_second=rate, writes_per_second=rate, clock=fake.clock, sleep=fake.sleep
    )
    for _ in range(burst):
        limiter.acquire(kind)
    assert not fake.sleeps
    limiter.acquire("write" if kind == "read" else "read")
    assert not fake.sleeps
    limiter.acquire(kind)
    assert fake.clock() == pytest.approx(1 / rate)


@given(
    attempt=st.integers(1, 100),
    seed=st.integers(),
    base=st.floats(min_value=0, max_value=8),
    cap=st.floats(min_value=0, max_value=120),
)
def test_retry_delay_bounds(attempt: int, seed: int, base: float, cap: float) -> None:
    policy = RetryPolicy(base_delay=base, max_delay=cap)
    bound = min(cap, base * 2 ** (attempt - 1))
    delay = policy.delay(attempt, rng=random.Random(seed))
    assert 0 <= delay <= bound
    assert delay == random.Random(seed).uniform(0, bound)


@pytest.mark.parametrize(
    ("retry_after", "expected"), [(None, 0.0), (0.5, 0.5), (59.0, 59.0), (1000.0, 300.0)]
)
def test_retry_after_respected_and_capped(retry_after: float | None, expected: float) -> None:
    assert RetryPolicy().max_attempts == 8
    policy = RetryPolicy(base_delay=0)
    assert policy.delay(1, retry_after=retry_after, rng=random.Random(0)) == expected


def test_retry_delay_handles_large_attempts() -> None:
    policy = RetryPolicy()
    assert policy.delay(10**6, rng=random.Random(0)) == random.Random(0).uniform(0, 64)


@pytest.mark.parametrize("attempt", [0, -1])
def test_retry_delay_rejects_attempts_before_first_retry(attempt: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy().delay(attempt, rng=random.Random(0))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("0", 0.0),
        (" 120\t", 120.0),
        ("00300", 300.0),
        ("301", 301.0),
        ("-3", None),
        ("+3", None),
        ("1.5", None),
        ("inf", None),
        ("9" * 400, None),
        ("\uff11\uff12", None),
        ("broken", None),
        ("Sun, 06 Nov 1994 08:49:37 GMT", 37.0),
        ("Sunday, 06-Nov-94 08:49:37 GMT", 37.0),
        ("Sun Nov  6 08:49:37 1994", 37.0),
        ("Sun, 06 Nov 1994 08:48:00 GMT", 0.0),
        ("Sun, 32 Nov 1994 08:49:37 GMT", None),
        ("Sun, 06 Nov 1994 08:49:37 GMT trailing", None),
        ("Sun, 06 Nov 1994 08:49:37 BAD", None),
    ],
)
def test_parse_retry_after_seconds_and_http_date(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=datetime(1994, 11, 6, 8, 49, tzinfo=UTC)) == expected


def test_parse_retry_after_treats_naive_reference_time_as_utc() -> None:
    header = "Sun, 06 Nov 1994 08:49:37 GMT"
    assert parse_retry_after(header, now=datetime(1994, 11, 6, 8, 49)) == 37


def test_parse_retry_after_rejects_obsolete_year_out_of_range() -> None:
    assert parse_retry_after("Sunday, 06-Nov-99 08:49:37 GMT", now=datetime.min) is None


def test_parse_retry_after_four_digit_year_is_literal() -> None:
    assert parse_retry_after("Mon, 01 Jan 0001 00:00:01 GMT", now=datetime.min) == 1
    assert parse_retry_after("Sun, 01 Jan 0000 00:00:01 GMT", now=datetime.min) is None


def test_parse_retry_after_obsolete_date_uses_full_fifty_year_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fifty_years = (datetime(2076, 1, 1, tzinfo=UTC) - now).total_seconds()
    assert parse_retry_after("Wednesday, 01-Jan-76 00:00:00 GMT", now=now) == fifty_years
    assert parse_retry_after("Wednesday, 01-Jan-76 00:00:01 GMT", now=now) == 0
