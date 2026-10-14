"""
A meeting record regenerated from Postgres is the record ingestion wrote.

The rebuild gap measured on 2026-10-13 was entirely meeting-record facts
(ingest/records.py). These tests are the proof that closing it reproduces the
same facts, not merely the same number of facts.
"""
from __future__ import annotations

from brahmastra import db
from brahmastra.ingest import assemble, records
from brahmastra.ingest import graph_record as gr
from brahmastra.ingest.store import Transcript
from tests.test_meeting_record import TRANSCRIPT, store, understood  # noqa: F401  (fixtures)

FIELDS = ("subject_text", "subject_type", "relation", "object_text", "object_type",
          "source_quote", "source_note_id")


def _record_facts(tid):
    note = gr.record_note_id(tid)
    return sorted(tuple(str(t.get(f) or "") for f in FIELDS)
                  for t in db.get_all_triples() if t.get("source_note_id") == note)


def _ingest(store):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT, occurred_at="2026-09-01"))
    assemble.process_transcript(tid, store=store)
    return tid


def test_regenerating_reproduces_exactly_what_ingestion_wrote(store, understood):  # noqa: F811
    tid = _ingest(store)
    written = _record_facts(tid)
    assert written, "ingestion should have written a record"

    db.delete_triples_for_note(gr.record_note_id(tid))       # the graph is lost
    assert _record_facts(tid) == []

    report = records.regenerate()
    assert report["errors"] == [] and report["records"] == 1
    assert _record_facts(tid) == written


def test_regenerating_twice_changes_nothing(store, understood):  # noqa: F811
    tid = _ingest(store)
    records.regenerate()
    first = _record_facts(tid)
    records.regenerate()
    assert _record_facts(tid) == first


def test_a_rejected_finding_stays_out_of_a_regenerated_record(store, understood):  # noqa: F811
    tid = _ingest(store)
    decision = next(a for a in store.get_artifacts(transcript_id=tid) if a["kind"] == "decision")
    store.reject_artifact(decision["id"], "wrong")
    records.regenerate()
    subjects = {f[0] for f in _record_facts(tid)}
    assert decision["statement"] not in subjects
