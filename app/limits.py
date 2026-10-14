"""Bounds on model usage (OWASP LLM10, unbounded consumption).

- RateLimiter: token bucket per client for the three endpoints that call the model (interpret, refine, advise).
- DailyBudget: stops model calls once today's estimated spend reaches the cap; the app keeps
  working through the manual editor. Spend comes from the per-call cost estimate in telemetry.

Both are in-process; with several replicas they would move to Redis (same interface).
"""

from __future__ import annotations

import threading
import time
from datetime import date


class RateLimiter:
    def __init__(self, per_minute: int):
        self._capacity = max(per_minute, 1)
        self._refill_per_s = self._capacity / 60.0
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, seconds_until_next_token)."""
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(self._capacity), now))
            tokens = min(self._capacity, tokens + (now - updated) * self._refill_per_s)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1 - tokens) / self._refill_per_s


class DailyBudget:
    def __init__(self, limit_usd: float, today=date.today):
        self.limit_usd = limit_usd
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._spent = 0.0

    def _roll(self) -> None:
        if self._today() != self._day:
            self._day, self._spent = self._today(), 0.0

    def available(self) -> bool:
        with self._lock:
            self._roll()
            return self._spent < self.limit_usd

    def spend(self, usd: float) -> None:
        with self._lock:
            self._roll()
            self._spent += usd

    @property
    def spent_today(self) -> float:
        with self._lock:
            self._roll()
            return round(self._spent, 6)
