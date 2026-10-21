"""
Notes router — CRUD for notes in SQLite.
"""

from __future__ import annotations

from typing import Any, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from brahmastra import db, usage
from brahmastra.accounts import current_identity

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteCreate(BaseModel):
    id: str
    title: str
    content: str
    last_edited: Optional[str] = None
    extraction_status: Optional[str] = None  # "pending" | "done" | "error"


@router.get("")
async def list_notes(
    status: str | None = None,
    q: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    List notes, or search them when `q` is given.

    Search was reachable only over MCP until now: this route accepted `?q=` in
    the sense that FastAPI ignored it, returning every note in last_edited
    order. That looks exactly like a search returning everything as a weak
    match, so the flagship feature appeared present and broken rather than
    absent.

    Results come back in RELEVANCE order and must not be re-sorted -- on the
    hybrid backends this is the fused BM25-plus-vector ranking, and reordering
    by date discards the entire point of it.
    """
    if q and q.strip():
        return _with_authors(db.search_notes(q, limit=limit))
    return _with_authors(db.get_notes(status=status))


def _with_authors(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `added_by` (a user id, or None) so the dashboard can offer Remove
    only where the server would allow it. The server still decides."""
    authors = usage.authors_of([n["id"] for n in notes if n.get("id")])
    for n in notes:
        n["added_by"] = authors.get(n.get("id") or "")
    return notes


@router.get("/{note_id}")
async def get_note(note_id: str) -> dict[str, Any]:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return _with_authors([note])[0]


@router.post("", status_code=201)
async def create_note(body: NoteCreate) -> dict[str, Any]:
    # Honor the caller's extraction_status if provided; default to pending
    # so the pipeline picks it up for LLM extraction.
    mark_pending = body.extraction_status != "done"
    me = current_identity()
    usage.charge_note(me, body.content)      # raises LimitReached -> 429
    try:
        db.upsert_note(
            id=body.id,
            title=body.title,
            content=body.content,
            last_edited=body.last_edited,
            mark_pending=mark_pending,
            # The REST route is what the Next.js dashboard posts to. A future
            # non-UI caller of POST /notes should send its own source instead.
            source="ui",
        )
    except Exception:
        usage.refund(me, "notes", 1)
        usage.refund(me, "words", usage.words_in(body.content))
        raise
    return db.get_note(body.id)  # type: ignore[return-value]


@router.delete("/{note_id}", status_code=204)
async def delete_note(note_id: str, request: Request, background_tasks: BackgroundTasks) -> None:
    """Remove a note and the facts taken from it. Admins: any note; members:
    their own. A note from Notion is also kept out of later syncs. See
    brahmastra/removals.py."""
    from brahmastra import removals

    actor, ip = removals.who(request)
    try:
        removals.remove_note(note_id, actor=actor, ip=ip)
    except removals.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except removals.NotAllowed as e:
        raise HTTPException(status_code=403, detail=str(e))
    from brahmastra.workspace import current_workspace

    background_tasks.add_task(removals.refresh_graph, current_workspace())
