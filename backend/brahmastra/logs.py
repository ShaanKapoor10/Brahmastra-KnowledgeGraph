"""
Structured logs: JSON lines, a W3C trace on every request, and nothing private.

WHAT A LOG LINE MAY CONTAIN is decided by an ALLOWLIST, not a denylist.
A denylist ("strip anything that looks like a key") fails open: the first new
field nobody thought of -- a prompt, a note body, a search query -- goes out.
So a record carries only the fields named in ALLOWED_FIELDS; any other `extra`
passed to a logger is DROPPED, and messages pass through the secret scrubber
in sessions.py as a second layer. Never logged, by construction:

    request and response bodies    note text, transcripts, answers
    query strings                  search text is in them (GET /notes?q=...)
    headers                        Authorization, Cookie, X-API-Key
    prompts and model replies      they quote the user's notes
    email addresses                identities are logged as opaque user ids

Uvicorn's own access log is OFF in the container (Dockerfile) because it logs
raw paths with query strings; the request log here records the ROUTE TEMPLATE
(`/notes/{note_id}`), never the concrete path.

TRACING follows the W3C Trace Context standard (`traceparent:
00-<trace-id>-<span-id>-<flags>`). An incoming traceparent is continued (the
dashboard's proxy starts one per request); otherwise one is started here. Each
request gets its own span id, every log line during it carries both ids, and
the response returns `traceparent`, so one id finds every line a request wrote
across the dashboard and the API.

    LOG_FORMAT   json (default in production) | text
    LOG_LEVEL    INFO
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import time
from contextvars import ContextVar
from typing import Any

ALLOWED_FIELDS = frozenset({
    "event", "method", "route", "status", "duration_ms", "workspace", "user_id", "identity",
    "error_type", "stage", "component", "state", "count", "seconds", "outcome", "version",
})

_trace: ContextVar[tuple[str, str] | None] = ContextVar("brahmastra_trace", default=None)
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


# ---------------------------------------------------------------- tracing

def parse_traceparent(value: str | None) -> str | None:
    """The trace id from a valid W3C traceparent, else None (invalid ones are ignored, per spec)."""
    m = _TRACEPARENT.match((value or "").strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return m.group(1)


def start_span(incoming: str | None) -> tuple[str, str]:
    trace_id = parse_traceparent(incoming) or secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    _trace.set((trace_id, span_id))
    return trace_id, span_id


def current_trace() -> tuple[str, str] | None:
    return _trace.get()


def traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


# ---------------------------------------------------------------- formatting

def _scrub(text: str) -> str:
    try:
        from brahmastra.sessions import redact

        return redact(text)
    except Exception:                                            # noqa: BLE001
        return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": _scrub(record.getMessage())[:2000],
        }
        trace = _trace.get()
        if trace:
            out["trace_id"], out["span_id"] = trace
        for key in ALLOWED_FIELDS:
            if key in record.__dict__:
                out[key] = record.__dict__[key]
        if record.exc_info:
            # The exception TYPE and where it happened; not its message, which
            # can quote whatever the failing code was holding.
            exc_type = record.exc_info[0]
            out["error_type"] = exc_type.__name__ if exc_type else "Exception"
            tb = record.exc_info[2]
            while tb and tb.tb_next:
                tb = tb.tb_next
            if tb:
                out["at"] = f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}"
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        trace = _trace.get()
        prefix = f"[{trace[0][:8]}] " if trace else ""
        extras = " ".join(f"{k}={record.__dict__[k]}" for k in sorted(ALLOWED_FIELDS) if k in record.__dict__)
        line = f"{record.levelname:<7} {record.name}: {prefix}{_scrub(record.getMessage())}"
        return f"{line} {extras}".rstrip()


_configured = False


def configure() -> None:
    """Install the formatter on the root logger once per process."""
    global _configured
    # The test suite keeps pytest's own log capture (tests/conftest.py sets this).
    if _configured or os.environ.get("BRAHMASTRA_LOGS", "").lower() == "off":
        return
    production = os.environ.get("BRAHMASTRA_ENV", "").lower() in {"production", "prod"}
    fmt = os.environ.get("LOG_FORMAT", "json" if production else "text").lower()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # Raw paths with query strings: replaced by the request log below.
    logging.getLogger("uvicorn.access").disabled = True
    # Libraries that log request URLs (which can carry API keys) at INFO.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


request_log = logging.getLogger("brahmastra.request")
