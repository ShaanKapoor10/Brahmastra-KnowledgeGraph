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

IN MEMORY, PER PROCESS. With several backend workers each enforces its own
count, so the effective limit is the limit times the number of workers. That is
acceptable for a team install (one or two workers); a multi-host deployment
would move this to Postgres or Redis.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULTS = {"ask": "30/m", "pipeline": "20/h", "ingest": "30/h", "write": "300/m", "read": "1200/m"}

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = {}


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
    with _lock:
        _hits.clear()
