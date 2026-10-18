"""
The limits that cap spending hold across backend workers.

Counted per process, two workers used to double every limit. The ask,
pipeline and ingest buckets now count in the notes database, so a second
"worker" -- simulated by dropping all in-process state between calls --
sees the first one's calls.
"""
from __future__ import annotations

import pytest

from brahmastra import ratelimit


@pytest.fixture
def limits(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "rate.db"))
    monkeypatch.setenv("RATE_LIMITS", "ask=3/m,read=2/m")
    ratelimit.reset()
    yield
    ratelimit.reset()


def _new_worker():
    """What a second process starts with: no in-memory counts, no cached store."""
    ratelimit._hits.clear()
    ratelimit._windows = None


def test_the_ask_limit_is_shared_between_workers(limits):
    for _ in range(3):
        assert ratelimit.check("u1", "POST", "/ask") is None
        _new_worker()
    wait = ratelimit.check("u1", "POST", "/ask")
    assert wait is not None and 1 <= wait <= 61
    # Per person: someone else still has their own allowance.
    assert ratelimit.check("u2", "POST", "/ask") is None


def test_cheap_limits_stay_in_memory(limits):
    """Reads guard against loops, not cost: no database round trip per read."""
    assert ratelimit.check("u1", "GET", "/notes") is None
    assert ratelimit.check("u1", "GET", "/notes") is None
    assert ratelimit.check("u1", "GET", "/notes") is not None
    _new_worker()
    assert ratelimit.check("u1", "GET", "/notes") is None


def test_an_unreachable_database_falls_back_instead_of_refusing(limits, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("database unreachable")

    monkeypatch.setattr(ratelimit, "_shared_hit", broken)
    for _ in range(3):
        assert ratelimit.check("u1", "POST", "/ask") is None      # still served...
    assert ratelimit.check("u1", "POST", "/ask") is not None      # ...and still limited


from tests._pg import reachable_dsn  # noqa: E402

PG_DSN = reachable_dsn()


@pytest.mark.skipif(PG_DSN is None, reason="BRAHMASTRA_TEST_POSTGRES_DSN not set or not reachable")
def test_the_shared_limit_holds_on_postgres_too(monkeypatch):
    """The deployed arrangement: counts live in Postgres beside the notes."""
    # Only the counters' table is pointed at the throwaway database; the
    # environment's store choice is left alone (conftest guards it).
    import brahmastra.sidecar as sidecar

    monkeypatch.setattr(sidecar, "note_backend", lambda: "postgres")
    monkeypatch.setenv("POSTGRES_DSN", PG_DSN)
    # An hour, not a minute: a connection can be slow (Windows tries IPv6
    # localhost first), and calls straddling a window boundary reset the count.
    monkeypatch.setenv("RATE_LIMITS", "ask=2/h")
    ratelimit.reset()
    try:
        who = "pg-user"
        assert ratelimit.check(who, "POST", "/ask") is None
        _new_worker()
        assert ratelimit.check(who, "POST", "/ask") is None
        _new_worker()
        assert ratelimit.check(who, "POST", "/ask") is not None
    finally:
        ratelimit.reset()
