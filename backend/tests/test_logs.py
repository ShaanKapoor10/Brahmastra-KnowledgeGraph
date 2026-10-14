"""
Structured logs carry a W3C trace and only allowlisted fields (brahmastra/logs.py).
"""
from __future__ import annotations

import importlib
import json
import logging

import pytest

from brahmastra import logs


def _record(msg="hello", **extra):
    rec = logging.LogRecord("brahmastra.test", logging.INFO, __file__, 1, msg, None, None)
    rec.__dict__.update(extra)
    return rec


def test_only_allowlisted_fields_reach_a_log_line():
    line = json.loads(logs.JsonFormatter().format(_record(
        route="/notes/{note_id}", status=200, prompt="the user's private note", body="secret text",
        email="ana@example.com", authorization="Bearer bra_x")))
    assert line["route"] == "/notes/{note_id}" and line["status"] == 200
    for leaked in ("prompt", "body", "email", "authorization"):
        assert leaked not in line, f"{leaked} must never be logged"


def test_secrets_in_a_message_are_scrubbed_as_a_second_layer():
    key = "gsk_" + "A" * 48
    line = json.loads(logs.JsonFormatter().format(_record(f"calling with {key}")))
    assert key not in line["msg"]


def test_an_exception_logs_its_type_and_place_not_its_message():
    try:
        raise ValueError("the note said: our acquisition target is Acme")
    except ValueError:
        import sys
        rec = _record("failed")
        rec.exc_info = sys.exc_info()
    line = json.loads(logs.JsonFormatter().format(rec))
    assert line["error_type"] == "ValueError" and "Acme" not in json.dumps(line)


def test_a_valid_traceparent_is_continued_and_an_invalid_one_replaced():
    good = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert logs.parse_traceparent(good) == "4bf92f3577b34da6a3ce929d0e0e4736"
    for bad in ("", "garbage", "00-" + "0" * 32 + "-00f067aa0ba902b7-01", "01-abc-def-01"):
        assert logs.parse_traceparent(bad) is None


def test_every_response_carries_a_traceparent_and_each_request_is_logged(monkeypatch, tmp_path, caplog):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "logs.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    client = TestClient(main.app)

    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    with caplog.at_level(logging.INFO, logger="brahmastra.request"):
        r = client.get("/notes?q=our%20secret%20merger", headers={"traceparent": incoming})
    assert r.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    rec = next(x for x in caplog.records if x.name == "brahmastra.request")
    assert rec.route == "/notes" and rec.status == 200 and rec.method == "GET"
    line = logs.JsonFormatter().format(rec)
    assert "merger" not in line, "the query string must never be logged"
