"""Ordered, one-shot and repeating fault rules with deterministic timing."""

from __future__ import annotations

from _thread import RLock
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import httpx


class SimulatedCrash(BaseException):
    """Emulate abrupt process death outside application Exception handlers."""


@dataclass(slots=True)
class Fault:
    """A request predicate with an action and a bounded number of applications."""

    when: Callable[[httpx.Request], bool]
    action: Literal[
        "status", "transport_error", "drop_response", "crash_before", "crash_after", "delay"
    ]
    status: int = 500
    reason: str = "backendError"
    times: int = 1
    skip: int = 0
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class CallRecord:
    """A request and its server status, or None if handling never produced a response."""

    method: str
    path: str
    query: httpx.QueryParams
    status: int | None


class FaultPlan:
    """Evaluate the first applicable rule, in registration order, per request."""

    def __init__(self, lock: RLock | None = None) -> None:
        self._lock = RLock() if lock is None else lock
        self._rules: list[Fault] = []

    def add(self, fault: Fault) -> None:
        """Register a fault with independent counters for its skip and times budget."""
        with self._lock:
            if fault.skip < 0 or fault.times < 0 or fault.seconds < 0:
                raise ValueError("Fault counters and duration must be nonnegative")
            if fault.times:
                self._rules.append(replace(fault))

    def clear(self) -> None:
        """Remove every pending fault."""
        with self._lock:
            self._rules.clear()

    @staticmethod
    def is_write(request: httpx.Request) -> bool:
        """Identify mutation methods apart from the token and ID generation endpoints."""
        return request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path not in {
            "/token",
            "/drive/v3/files/generateIds",
        }

    def _next(self, request: httpx.Request) -> Fault | None:
        for index, fault in enumerate(self._rules):
            if not fault.when(request):
                continue
            if fault.skip:
                fault.skip -= 1
                continue
            fault.times -= 1
            if not fault.times:
                self._rules.pop(index)
            return fault
        return None
