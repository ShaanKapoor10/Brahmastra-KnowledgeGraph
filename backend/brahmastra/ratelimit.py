"""
Per-user request limits, so one account cannot run up the team's LLM bill.

Several routes spend money on every call: /ask answers with an LLM, the
pipeline extracts with one, and ingesting a session runs several passes. Signed
in, a script or a stuck browser tab can call them in a loop. These limits cap
that per person, and say when to retry (HTTP 429 with Retry-After).

    bucket     routes                                   default
    ask        POST /ask                                30 per minute
    pipeline   POST /pipeline/*                         20 per hour
    ingest     POST /ingest/transcripts*, reprocess     30 per hour
    write      any other POST/PUT/PATCH/DELETE          300 per minute
    read       everything else                          1200 per minute

Override with RATE_LIMITS, e.g. RATE_LIMITS="ask=60/m,pipeline=5/h"; set
RATE_LIMITS=off to disable. Service keys and anonymous development mode are not
limited: the scheduler and the MCP server use the service key, and throttling
them would stall the pipeline rather than protect anything.

WHERE THE COUNTS LIVE. The limits that cap spending -- ask, pipeline, ingest --
are counted in the database that holds the notes (table rate_windows), so they
hold across every backend worker and host: counted per process, two workers
used to double each limit. Each is an atomic fixed-window counter (one upsert
per call), so two workers can never both take the last slot. Read and write
limits stay in memory, per process: they guard against runaway loops, not
cost, and a database round trip on every read would cost more than it saves.

If the database cannot be reached, the shared buckets fall back to counting in
memory rather than refusing: a limiter must never be what takes the API down.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from brahmastra.sidecar import SidecarStore

_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULTS = {"ask": "30/m", "pipeline": "20/h", "ingest": "30/h", "write": "300/m", "read": "1200/m"}

# Buckets that cost money per call, counted across workers in the database.
SHARED_BUCKETS = frozenset({"ask", "pipeline", "ingest"})

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = {}
_log = logging.getLogger("brahmastra.ratelimit")


class _Windows(SidecarStore):
    """Per-user, per-bucket counters for fixed windows. Not workspace data."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS rate_windows (
        who           TEXT    NOT NULL,
        bucket        TEXT    NOT NULL,
        window_start  BIGINT  NOT NULL,
        hits          INTEGER NOT NULL,
        PRIMARY KEY (who, bucket, window_start)
    )
    """

    def hit(self, who: str, bucket: str, window_start: int) -> int:
        """Count one call and return the window's total, atomically."""
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO rate_windows (who, bucket, window_start, hits) VALUES (?, ?, ?, 1) "
                "ON CONFLICT (who, bucket, window_start) DO UPDATE SET hits = rate_windows.hits + 1 "
                "RETURNING hits"), (who, bucket, window_start))
            row = cur.fetchone()
            hits = int(self._dict(row)["hits"])
            if hits == 1:
                # A new window: drop windows older than a day, so the table
                # stays the size of the current traffic.
                cur.execute(self._ph("DELETE FROM rate_windows WHERE window_start < ?"),
                            (window_start - 86400,))
        return hits


_windows: _Windows | None = None


def _shared_hit(who: str, bucket: str, allowed: int, window: int) -> int | None:
    """None if allowed, else seconds to wait; raises if the database is unreachable."""
    global _windows
    if _windows is None:
        _windows = _Windows(workspace="default")     # the table is not per workspace
    now = time.time()
    start = int(now // window) * window
    if _windows.hit(who, bucket, start) <= allowed:
        return None
    return max(1, int(start + window - now) + 1)


def _parse(spec: str) -> tuple[int, int]:
    count, unit = spec.strip().split("/")
    return int(count), _UNIT[unit.strip()[0]]


def limits() -> dict[str, tuple[int, int]] | None:
    raw = os.environ.get("RATE_LIMITS", "").strip()
    if raw.lower() in {"off", "0", "none"}:
        return None
    merged = dict(DEFAULTS)
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, _, spec = part.partition("=")
        if name.strip() in merged and spec:
            merged[name.strip()] = spec
    return {name: _parse(spec) for name, spec in merged.items()}


def bucket_for(method: str, path: str) -> str:
    if method == "POST" and path == "/ask":
        return "ask"
    if method == "POST" and path.startswith("/pipeline"):
        return "pipeline"
    if method == "POST" and path.startswith("/ingest/transcripts"):
        return "ingest"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return "write"
    return "read"


def check(who: str, method: str, path: str) -> int | None:
    """None if allowed (and counted), else the seconds until the next call is allowed."""
    table = limits()
    if table is None:
        return None
    bucket = bucket_for(method, path)
    allowed, window = table[bucket]
    if bucket in SHARED_BUCKETS:
        try:
            return _shared_hit(who, bucket, allowed, window)
        except Exception as exc:                              # noqa: BLE001
            _log.warning("shared rate limit unavailable; counting in this process",
                         extra={"event": "ratelimit", "outcome": type(exc).__name__})
    now = time.monotonic()
    with _lock:
        q = _hits.setdefault((who, bucket), deque())
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= allowed:
            return max(1, int(window - (now - q[0])) + 1)
        q.append(now)
    return None


def reset() -> None:
    """Forget every count: in memory, and the shared table (tests only)."""
    global _windows
    with _lock:
        _hits.clear()
    try:
        store = _windows or _Windows(workspace="default")
        store.init_schema()
        with store._cursor() as cur:
            cur.execute("DELETE FROM rate_windows")
    except Exception:                                         # noqa: BLE001
        pass
    _windows = None
