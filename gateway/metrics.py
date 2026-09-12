"""In-memory request metrics and a bounded request log.

No database: a restart resets everything, which is explicitly acceptable.
The log never stores prompts, tool outputs, headers or API keys.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

DEFAULT_LOG_CAPACITY = 500
DEFAULT_LOG_PAGE = 100


@dataclass
class RequestRecord:
    """A single client-visible request summary (no secrets, no payloads)."""

    time: float
    request_id: str
    logical_model: str = ""
    reasoning_level: str | None = None
    provider: str = ""
    upstream_model: str = ""
    protocol: str = ""
    stream: bool = False
    status: int = 0
    latency_ms: float = 0.0
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_type is None and 200 <= self.status < 400

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "time_iso": _iso(self.time),
            "request_id": self.request_id,
            "logical_model": self.logical_model,
            "reasoning_level": self.reasoning_level,
            "provider": self.provider,
            "upstream_model": self.upstream_model,
            "protocol": self.protocol,
            "stream": self.stream,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "error_type": self.error_type,
            "ok": self.ok,
        }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


@dataclass
class Metrics:
    """Thread-safe counters plus a bounded ring buffer of recent requests."""

    capacity: int = DEFAULT_LOG_CAPACITY
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _log: deque = field(init=False, repr=False)
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    _latency_sum_ms: float = 0.0
    _latency_count: int = 0
    by_model: Counter = field(default_factory=Counter)
    by_provider: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self._log = deque(maxlen=self.capacity)

    def record(self, record: RequestRecord) -> None:
        with self._lock:
            self.total_requests += 1
            if record.ok:
                self.successful_requests += 1
            else:
                self.failed_requests += 1
            if record.latency_ms:
                self._latency_sum_ms += record.latency_ms
                self._latency_count += 1
            if record.logical_model:
                self.by_model[record.logical_model] += 1
            if record.provider:
                self.by_provider[record.provider] += 1
            self._log.append(record)

    def logs(self, limit: int = DEFAULT_LOG_PAGE) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._log)
        if limit and limit > 0:
            items = items[-limit:]
        return [r.to_dict() for r in reversed(items)]

    def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = [r for r in self._log if not r.ok]
        return [r.to_dict() for r in reversed(items[-limit:])]

    def recent_requests(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._log)
        return [r.to_dict() for r in reversed(items[-limit:])]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            avg = (
                self._latency_sum_ms / self._latency_count
                if self._latency_count
                else 0.0
            )
            return {
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "average_latency_ms": round(avg, 1),
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests_by_model": dict(self.by_model),
                "requests_by_provider": dict(self.by_provider),
            }
