"""
Regenerate every meeting record's facts from what Postgres already holds.

WHY THIS EXISTS. The graph is a cache: every fact in it must be recomputable
from the system of record, or a rebuild silently loses it. Measured on
2026-10-13 by content hash: a full rebuild reproduced 1,584 of production's
1,835 facts exactly, and the other 251 were ALL one meeting record's facts --
attended, decided_by, assigned_to, said_by, mentions, has_status. Ingestion
declares those straight into the graph (ingest/assemble._write_graph_record),
and extraction rightly skips the record note (extraction.CODE_WRITTEN_SOURCES),
so nothing ever wrote them again.

They need no model to come back. Everything the record is made of is SOURCE
data in the notes database:

    the transcript row         title, occurred_at       -> the meeting's name
    transcript_chunks          speakers, rendered turns -> attendance, who said each quote
    meeting_artifacts          kind, statement, owner, quote, status, about
    artifact_rejections        findings a person rejected stay out

and graph_record.record_triples is pure. So this reads those rows and writes
the same triples ingestion wrote, keyed to the same record note id.
tests/test_ingest_records.py proves "the same": it ingests a meeting, wipes the
graph, regenerates, and compares content hashes.

Run by the pipeline's full rebuild (pipeline.run_pipeline(full=True)), never
by an incremental run: on an incremental run the record is already current,
because ingestion wrote it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _StoredChunk:
    """Enough of segment.Chunk for speaker_of and attendance, rebuilt from a row."""

    index: int
    turns: list[Any] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)


def _chunk_from_row(row: dict[str, Any]) -> _StoredChunk:
    from brahmastra.ingest.segment import parse_turns

    speakers = row.get("speakers") or []
    if isinstance(speakers, str):
        try:
            speakers = json.loads(speakers)
        except ValueError:
            speakers = []
    # A chunk's text is its turns rendered as `[ts] Speaker: text` lines
    # (segment.Turn.rendered), which is exactly what the speaker-line parser reads.
    return _StoredChunk(index=int(row.get("idx", 0)), turns=parse_turns(row.get("text") or "", "speaker"),
                        speakers=list(speakers))


def triples_for(transcript_id: str) -> tuple[str, list[dict[str, Any]]]:
    """(record note id, its triples) for one transcript, from stored rows only."""
    from brahmastra.ingest import graph_record as gr
    from brahmastra.ingest.evidence import speaker_of
    from brahmastra.ingest.store import get_ingest_store

    store = get_ingest_store()
    record = store.get_transcript(transcript_id) or {}
    artifacts = store.get_artifacts(transcript_id=transcript_id, limit=1_000_000)
    rejected = store.rejected_ids(transcript_id)
    artifacts = [a for a in artifacts if a.get("id") not in rejected]
    chunks = [_chunk_from_row(r) for r in store.get_chunks(transcript_id)]
    by_index = {c.index: c for c in chunks}
    artifacts = [{**a, "said_by": speaker_of(a.get("quote") or "", by_index.get(a.get("chunk_index")))}
                 for a in artifacts]
    meeting = gr.meeting_name(record.get("title") or transcript_id, record.get("occurred_at"))
    participants = sorted({s for c in chunks for s in c.speakers})
    note_id = gr.record_note_id(transcript_id)
    triples = gr.record_triples(meeting, participants, artifacts)
    for t in triples:
        t["source_note_id"] = note_id
    return note_id, triples


def regenerate() -> dict[str, Any]:
    """Rewrite every meeting record's facts in the bound workspace. Idempotent."""
    from brahmastra import db
    from brahmastra.ingest.store import get_ingest_store

    report: dict[str, Any] = {"transcripts": 0, "records": 0, "triples": 0, "errors": []}
    for t in get_ingest_store().list_transcripts(limit=1_000_000):
        report["transcripts"] += 1
        try:
            note_id, triples = triples_for(t["id"])
            if not triples:
                continue
            db.delete_triples_for_note(note_id)
            db.insert_triples(triples)
            report["records"] += 1
            report["triples"] += len(triples)
        except Exception as exc:                                 # noqa: BLE001
            report["errors"].append({"transcript": t["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
    return report
