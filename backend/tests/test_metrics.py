"""
/metrics: standard Prometheus text, route templates as labels, internal only.
"""
from __future__ import annotations

import importlib
import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "metrics.db"))
    monkeypatch.setenv("BRAHMASTRA_OPS_STATE", str(tmp_path / "ops"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    return TestClient(main.app)


def _metric_lines(text, name):
    return [line for line in text.splitlines() if line.startswith(name)]


def test_requests_are_labelled_by_route_template_never_by_path(client):
    client.get("/notes/some-private-note-id")
    client.get("/notes?q=private search words")
    body = client.get("/metrics").text
    lines = _metric_lines(body, "brahmastra_http_requests_total")
    assert any('route="/notes/{note_id}"' in line for line in lines)
    assert "some-private-note-id" not in body and "private search words" not in body


def test_latency_buckets_are_dense_around_the_objective(client):
    client.get("/notes")
    body = client.get("/metrics").text
    for le in ("1.25", "1.5", "1.75"):
        assert any(f'le="{le}"' in line for line in _metric_lines(body, "brahmastra_http_request_duration_seconds_bucket"))


def test_metrics_refuse_anything_that_came_through_the_proxy(client):
    assert client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 404


def test_backup_freshness_is_exported_from_the_backup_jobs_state(client, tmp_path):
    ops = tmp_path / "ops"
    ops.mkdir()
    now = int(time.time())
    (ops / "backup.json").write_text(json.dumps({"outcome": "ok", "last_attempt": now, "last_success": now}))
    (ops / "restore_test.json").write_text(json.dumps({"outcome": "failed", "last_attempt": now,
                                                       "last_success": now - 8 * 86400}))
    body = client.get("/metrics").text
    value = next(line.split()[-1] for line in _metric_lines(body, "brahmastra_backup_last_success_timestamp_seconds")
                 if not line.startswith("#"))
    assert float(value) == float(now)
    assert "brahmastra_backup_last_attempt_ok 1.0" in body
    assert "brahmastra_restore_test_last_attempt_ok 0.0" in body


def test_graph_and_snapshot_state_are_exported(client):
    from brahmastra import graph_snapshot

    graph_snapshot.store().save({"built_at": "2026-10-14T09:00:00", "graph": {"nodes": [], "edges": []}}, [], {})
    body = client.get("/metrics").text
    assert "brahmastra_graph_up 1.0" in body
    assert any('workspace="default"' in line for line in _metric_lines(body, "brahmastra_graph_snapshot_bytes"))
    assert _metric_lines(body, "brahmastra_build_info")


def test_metrics_are_open_to_the_internal_scraper_without_a_token(client, monkeypatch):
    monkeypatch.delenv("BRAHMASTRA_ALLOW_ANONYMOUS")
    monkeypatch.setenv("BRAHMASTRA_API_KEY", "a-real-key-for-this-test-0123456789")
    assert client.get("/metrics").status_code == 200
    assert client.get("/notes").status_code == 401
