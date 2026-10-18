"""
A rebuild is idempotent, and a fingerprint says which kind of fact changed.
"""
from __future__ import annotations

from brahmastra import db, rebuild
from brahmastra.ingest import assemble
from brahmastra.ingest.store import Transcript
from tests.test_meeting_record import TRANSCRIPT, store, understood  # noqa: F401  (fixtures)


def _fake_extraction(monkeypatch):
    """Deterministic extraction: one fact per note, so rebuilds can be compared."""
    import brahmastra.extraction as extraction

    def fake(title, content, *a, **k):
        return [{"subject_text": title, "subject_type": "concept", "relation": "related_to",
                 "object_text": "Brahmastra", "object_type": "product", "confidence": 0.9,
                 "source_quote": content[:40]}]
    monkeypatch.setattr(extraction, "_extract_with_llm", fake, raising=False)


def test_rebuilding_twice_gives_the_same_fingerprint(store, understood, monkeypatch):  # noqa: F811
    _fake_extraction(monkeypatch)
    db.upsert_note("n1", "Apollo", "Apollo uses Postgres.")
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT, occurred_at="2026-09-01"))
    assemble.process_transcript(tid, store=store)

    first = rebuild.rebuild_all()["default"]
    second = rebuild.rebuild_all()["default"]
    assert first["status"] == second["status"] == "ok", (first["failed_stages"], second["failed_stages"])
    assert first["fingerprint"] == second["fingerprint"]
    assert "meeting-record" in first["fingerprint"]["by_source"]
    assert "records" in first["timings_s"] and "extract" in first["timings_s"]


def test_a_lost_meeting_record_comes_back_in_a_rebuild(store, understood, monkeypatch):  # noqa: F811
    _fake_extraction(monkeypatch)
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT, occurred_at="2026-09-01"))
    assemble.process_transcript(tid, store=store)
    before = {ws: r["fingerprint"] for ws, r in rebuild.rebuild_all().items()}   # a complete graph

    from brahmastra.ingest import graph_record as gr
    db.delete_triples_for_note(gr.record_note_id(tid))          # the graph lost it
    assert rebuild.compare(before, rebuild.fingerprint_all())    # and the fingerprint says so

    after = {ws: r["fingerprint"] for ws, r in rebuild.rebuild_all().items()}
    assert rebuild.compare(before, after) == []


def test_the_comparison_names_the_source_that_differs():
    live = {"default": {"facts": 3, "sha256": "a", "by_source": {
        "mcp": {"facts": 2, "sha256": "m"}, "meeting-record": {"facts": 1, "sha256": "r"}}}}
    rebuilt = {"default": {"facts": 2, "sha256": "b", "by_source": {"mcp": {"facts": 2, "sha256": "m"}}}}
    assert rebuild.compare(live, rebuilt) == ["default/meeting-record: 1 facts live, 0 rebuilt"]
