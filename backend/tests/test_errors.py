"""
public_error (brahmastra/errors.py): what a failure may say to a client.

Errors are shown on purpose (the Diagnostics tab, a note's extraction error, a
sync that skipped a page), so the diagnosing part must survive: the exception
type, the status code, the provider's words. What must not: credentials in any
shape an SDK or driver writes them, and where the server's files live.
Keys and names below are invented.
"""
from __future__ import annotations

import pytest

from brahmastra.errors import public_error, scrub


@pytest.mark.parametrize("raw, secret", [
    ("connection to postgresql://brahmastra:Zq9pass@db.internal:5432/x failed", "Zq9pass"),
    ("connection to postgresql://brahmastra:Zq9@pass@db.internal:5432/x failed", "Zq9@pass"),
    ("invalid dsn: host=db.internal user=b password=Zq9pass dbname=x", "Zq9pass"),
    ("GET https://api.example.com/v1?user=b&password=Zq9pass&x=1 failed", "Zq9pass"),
    ("401 with header Authorization: Bearer fake0token0abcdef1234", "fake0token0abcdef1234"),
    ("Error code: 401 - Invalid API Key gsk_FAKEFAKEFAKEFAKEFAKEFAKE12", "gsk_FAKEFAKEFAKEFAKEFAKEFAKE12"),
    ("NEO4J_PASSWORD=Zq9passwordvalue was rejected", "Zq9passwordvalue"),
])
def test_credentials_never_survive(raw, secret):
    assert secret not in scrub(raw), scrub(raw)


def test_server_paths_go_but_the_file_name_stays():
    win = r"[Errno 2] No such file: 'C:\Users\alex\Desktop\app\backend\data\graph.db'"
    nix = "[Errno 13] Permission denied: '/home/alex/app/backend/data/graph.db'"
    for raw in (win, nix):
        out = scrub(raw)
        assert "alex" not in out and "graph.db" in out, out


def test_the_diagnosing_part_survives():
    """The words Diagnostics classifies errors by must still be there."""
    raw = ("Error code: 429 - Rate limit reached for model openai/gpt-oss-120b on tokens per day "
           "(TPD). See https://console.groq.com/docs/rate-limits")
    out = public_error(RuntimeError(raw))
    assert out.startswith("RuntimeError: Error code: 429")
    assert "tokens per day" in out and "https://console.groq.com/docs/rate-limits" in out

    from brahmastra.routers.diagnostics import classify_error
    assert classify_error(out)["kind"] == "quota"


def test_bounded_and_never_empty():
    assert len(public_error(ValueError("x" * 5000))) == 300
    assert len(public_error(ValueError("x" * 5000), limit=1000)) == 1000
    assert public_error(TimeoutError()) == "TimeoutError"


def test_an_extraction_failure_is_stored_and_returned_scrubbed(monkeypatch, tmp_path):
    from brahmastra import db, extraction
    from brahmastra.stores import reset_store

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "errors.db"))
    reset_store()
    db.init_db()
    db.upsert_note("n1", "A note", "Some content worth extracting.", mark_pending=True)

    def boom(*_a, **_k):
        raise ConnectionError("could not reach postgresql://b:Zq9pass@db.internal/x")

    monkeypatch.setattr(extraction, "_extract_with_llm", boom)
    out = extraction.extract_note(db.get_note("n1"))
    assert "Zq9pass" not in out["error"] and out["error"].startswith("ConnectionError")
    assert "Zq9pass" not in (db.get_note("n1") or {}).get("extraction_error", "")
    reset_store()
