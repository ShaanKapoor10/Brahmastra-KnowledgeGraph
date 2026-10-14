"""
Is the graph engine up, and what happens when it is not.

Notes live in Postgres; the graph lives in Neo4j and is DERIVED. So a Neo4j
outage must cost the graph features and nothing else: notes, note search and
/ask (answered from notes) keep working, the dashboard shows the last good
graph marked stale, and nothing hangs. On 2026-10-13 the opposite happened --
Aura suspended, every request that touched the graph waited out the driver's
timeouts, and the dashboard fell back to sample notes, which looked like data
loss.

THE CIRCUIT BREAKER. Every Cypher query goes through Neo4jStore._run, which
asks this breaker first. After BREAKER_FAILURES connection failures within
BREAKER_WINDOW seconds the circuit OPENS: for BREAKER_COOLDOWN seconds every
call fails at once with GraphUnavailable instead of waiting on a dead socket.
Then ONE call is let through (half-open); success closes the circuit, failure
re-opens it. Only connection-class errors count -- a bad query is a bug, not
an outage, and must not trip the breaker for everyone.

    NEO4J_BREAKER_FAILURES   3
    NEO4J_BREAKER_WINDOW     30   seconds
    NEO4J_BREAKER_COOLDOWN   30   seconds
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any


class GraphUnavailable(RuntimeError):
    """The graph engine cannot be reached now. Callers degrade; they do not crash."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def is_outage(exc: BaseException) -> bool:
    """Whether an exception means "the engine is unreachable" rather than "the query is wrong"."""
    if isinstance(exc, (GraphUnavailable, ConnectionError, TimeoutError)):
        return True
    try:
        from neo4j.exceptions import ServiceUnavailable, SessionExpired

        if isinstance(exc, (ServiceUnavailable, SessionExpired)):
            return True
    except ImportError:
        pass
    try:
        from neo4j.exceptions import ClientError

        # A transaction cut off by our own query timeout is the engine being too
        # slow to serve -- an outage from the caller's point of view.
        if isinstance(exc, ClientError) and "TransactionTimedOut" in (getattr(exc, "code", "") or ""):
            return True
    except ImportError:
        pass
    return isinstance(exc, OSError)


class CircuitBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._half_open_inflight = False
        self.last_error = ""
        self.opened_count = 0

    @property
    def threshold(self) -> int:
        return int(_env_float("NEO4J_BREAKER_FAILURES", 3))

    @property
    def window(self) -> float:
        return _env_float("NEO4J_BREAKER_WINDOW", 30)

    @property
    def cooldown(self) -> float:
        return _env_float("NEO4J_BREAKER_COOLDOWN", 30)

    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half-open" if time.monotonic() - self._opened_at >= self.cooldown else "open"

    def before_call(self) -> None:
        with self._lock:
            state = self._state_locked()
            if state == "open":
                left = self.cooldown - (time.monotonic() - self._opened_at)
                raise GraphUnavailable(f"graph engine unavailable (circuit open, retrying in {left:.0f}s): "
                                       f"{self.last_error}")
            if state == "half-open":
                if self._half_open_inflight:
                    raise GraphUnavailable(f"graph engine unavailable (probe in progress): {self.last_error}")
                self._half_open_inflight = True

    def success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at = None
            self._half_open_inflight = False

    def failure(self, exc: BaseException) -> None:
        with self._lock:
            now = time.monotonic()
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._half_open_inflight = False
            if self._opened_at is not None:          # the half-open probe failed
                self._opened_at = now
                self.opened_count += 1
                return
            self._failures = [t for t in self._failures if now - t < self.window] + [now]
            if len(self._failures) >= self.threshold:
                self._opened_at = now
                self.opened_count += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state_locked(), "recent_failures": len(self._failures),
                    "times_opened": self.opened_count, "last_error": self.last_error}

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at = None
            self._half_open_inflight = False
            self.last_error = ""


breaker = CircuitBreaker()
