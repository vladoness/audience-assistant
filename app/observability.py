"""Structured JSON logs with a request id, plus a small in-process metrics registry.

Every log line is one JSON object, so it can be shipped to any log store and filtered by
request_id, event, status or model. /api/metrics exposes counters and latency percentiles;
in production these would be exported to Prometheus/OpenTelemetry instead of kept in memory.
"""

from __future__ import annotations

import contextvars
import json
import logging
import statistics
import threading
import time
from collections import defaultdict, deque
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Metrics:
    """Thread-safe counters and bounded latency samples (last 1,000 per timer)."""

    def __init__(self, window: int = 1000):
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))

    def incr(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += value

    def observe(self, name: str, ms: float) -> None:
        with self._lock:
            self._timings[name].append(ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timings = {}
            for name, samples in self._timings.items():
                ordered = sorted(samples)
                timings[name] = {
                    "count": len(ordered),
                    "p50_ms": round(statistics.median(ordered), 1),
                    "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 1),
                    "max_ms": round(ordered[-1], 1),
                }
            return {"counters": dict(sorted(self._counters.items())), "timings": timings}
