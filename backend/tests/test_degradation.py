"""
A graph-engine outage costs the graph features and nothing else.

Unit-level; the end-to-end acceptance check (stop the Neo4j container, see the
banner, the stale graph and the /ask fallback) is in docs/RELIABILITY.md and
was run against the production compose stack.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

from brahmastra.graph_health import CircuitBreaker, GraphUnavailable, breaker  # noqa: E402


@pytest.fixture(autouse=True)
def _closed_breaker():
    breaker.reset()
    yield
    breaker.reset()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "degrade.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    return TestClient(main.app, raise_server_exceptions=False)


def _engine_down(monkeypatch, *names):
    import brahmastra.db as db_mod

    def dead(*a, **k):
        raise GraphUnavailable("graph engine unavailable: ServiceUnavailable")
    for name in names:
        monkeypatch.setattr(db_mod, name, dead)


def _take_snapshot():
    from brahmastra import graph_snapshot

    graph = {"built_at": "2026-10-14T09:00:00+00:00", "graph": {"nodes": [{"id": "Apollo"}], "edges": []},
             "stats": {"nodes": 1}}
    graph_snapshot.store().save(graph, [{"subject_text": "Apollo", "relation": "uses", "object_text": "Postgres"}],
                                {"triples_total": 1})


# ---- the breaker ------------------------------------------------------------------------

def test_the_breaker_opens_after_repeated_outages_and_then_fails_fast(monkeypatch):
    monkeypatch.setenv("NEO4J_BREAKER_FAILURES", "3")
    monkeypatch.setenv("NEO4J_BREAKER_COOLDOWN", "60")
    b = CircuitBreaker()
    for _ in range(3):
        b.before_call()
        b.failure(ConnectionError("refused"))
    assert b.state() == "open"
    with pytest.raises(GraphUnavailable, match="circuit open"):
        b.before_call()


def test_a_successful_probe_closes_the_breaker(monkeypatch):
    monkeypatch.setenv("NEO4J_BREAKER_FAILURES", "1")
    monkeypatch.setenv("NEO4J_BREAKER_COOLDOWN", "0")
    b = CircuitBreaker()
    b.failure(ConnectionError("refused"))
    assert b.state() == "half-open"
    b.before_call()          # the one probe is let through
    b.success()
    assert b.state() == "closed"


def test_a_bad_query_is_not_an_outage():
    from brahmastra.graph_health import is_outage

    assert is_outage(ConnectionError()) and is_outage(GraphUnavailable("x"))
    assert not is_outage(ValueError("syntax error in Cypher"))


# ---- the routes -------------------------------------------------------------------------

def test_the_graph_is_served_from_the_snapshot_marked_stale(client, monkeypatch):
    _take_snapshot()
    _engine_down(monkeypatch, "get_cached_graph", "get_all_triples", "get_db_stats")

    g = client.get("/graph")
    assert g.status_code == 200
    body = g.json()
    assert body["graph"]["nodes"] == [{"id": "Apollo"}]
    assert body["availability"]["served_from"] == "snapshot"
    assert body["availability"]["snapshot_built_at"] == "2026-10-14T09:00:00+00:00"

    t = client.get("/graph/triples")
    assert t.status_code == 200 and t.json()[0]["subject_text"] == "Apollo"
    assert t.headers["X-Brahmastra-Graph"] == "stale"

    s = client.get("/graph/stats")
    assert s.status_code == 200 and s.json()["availability"]["graph"] == "unavailable"


def test_with_no_snapshot_the_graph_routes_say_unavailable_instead_of_crashing(client, monkeypatch):
    _engine_down(monkeypatch, "get_cached_graph")
    r = client.get("/graph")
    assert r.status_code == 503 and r.json()["degraded"] == "graph_unavailable"


def test_other_graph_features_answer_503_with_a_reason(client, monkeypatch):
    _engine_down(monkeypatch, "search_entities", "get_entities", "get_cached_graph")
    r = client.get("/entities?q=sarah")
    assert r.status_code == 503
    assert "Notes, search and Ask still work" in r.json()["detail"]


def test_notes_keep_working_while_the_graph_is_down(client, monkeypatch):
    _engine_down(monkeypatch, "get_cached_graph", "get_all_triples", "get_db_stats", "get_entities")
    assert client.post("/notes", json={"id": "n1", "title": "t", "content": "Sarah works at Acme."}).status_code == 201
    assert client.get("/notes").status_code == 200


# ---- /ask --------------------------------------------------------------------------------

def test_ask_answers_from_the_notes_and_says_so(monkeypatch):
    from brahmastra import rag

    def down(*a, **k):
        raise GraphUnavailable("graph engine unavailable")

    monkeypatch.setattr(rag, "_answer_question_graph", down)
    monkeypatch.setattr(rag, "_answer_from_passages",
                        lambda q, ents: {"mode": "local", "source": "notes", "answer": "From the notes.",
                                         "entities": ents, "citations": []})
    r = rag.answer_question("Where is Sarah based?")
    assert r["answer"] == "From the notes."
    assert r["degraded"] == "graph_unavailable" and r["source"] == "notes"


def test_ask_does_not_hide_a_real_bug_behind_the_fallback(monkeypatch):
    from brahmastra import rag

    def bug(*a, **k):
        raise KeyError("a programming error")

    monkeypatch.setattr(rag, "_answer_question_graph", bug)
    with pytest.raises(KeyError):
        rag.answer_question("anything")


# ---- the snapshot ---------------------------------------------------------------------------

def test_a_pipeline_run_refreshes_the_snapshot(client, monkeypatch):
    import brahmastra.extraction as extraction
    from brahmastra import db, graph_snapshot
    from brahmastra.pipeline import run_pipeline

    monkeypatch.setattr(extraction, "_extract_with_llm", lambda title, content, *a, **k: [
        {"subject_text": "Apollo", "subject_type": "project", "relation": "uses", "object_text": "Postgres",
         "object_type": "technology", "confidence": 0.9, "source_quote": content[:30]}], raising=False)
    db.upsert_note("n1", "Apollo", "Apollo uses Postgres for storage.")
    result = run_pipeline(full=True)
    assert result["stages"]["snapshot"]["refreshed"] is True
    snap = graph_snapshot.store().load()
    assert snap and snap["triples"] and snap["bytes"] > 0
    again = run_pipeline(full=False)
    assert "error" not in again["stages"]["snapshot"]          # refreshed, or unchanged: never failed
