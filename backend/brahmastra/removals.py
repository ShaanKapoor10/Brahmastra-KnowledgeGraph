"""
Removing a note or a meeting: who may, what goes, and what is remembered.

WHO MAY. Admins may remove anything. A member may remove what they added: a
note recorded against them in `note_authors` (usage.remember_author), a
meeting recorded against them in `transcripts.submitted_by`. Viewers never
reach this module -- a DELETE needs the member role (auth.required_role).
Anything nobody is recorded against -- a Notion page, a note written before
authors were recorded, a meeting submitted with the service key -- is
therefore an admin's to remove. Failing closed is the point: "nobody owns it"
must not read as "anybody may delete it".

A removal costs nothing, so it is deliberately NOT a plan feature
(plans.route_feature): someone moved to a smaller plan can still clear away
what they added.

WHAT GOES. A note is removed from both halves of the store (CompositeStore
routes `delete_note` to Postgres and Neo4j, derived rows first). A meeting
goes with its passages and findings; its notes stay as ordinary notes unless
the caller asks for them to go too (assemble.drop_transcript). The graph built
from the facts is then rebuilt in the background (`refresh_graph`), so the
removed thing leaves the entities, topics and Ask straight away. A name that
OTHER notes still mention stays, correctly: it is still in the notes.

WHAT IS REMEMBERED. Two things, both SOURCE data -- a person's decision that
nothing can recompute:

  * The audit log names who removed what, when, from which workspace.
  * A note pulled from Notion is the copy of a page that still exists. Deleting
    only the copy meant the next sync found the page without a note and pulled
    it straight back in. `removed_sources` records "don't pull this page
    again"; sync skips it. The page in Notion is never touched.

Removal is permanent apart from restoring a backup: `removed_sources` and the
audit log are in Postgres, which is backed up, and the graph is rebuilt from
what remains.
"""
from __future__ import annotations

import time
from typing import Any

from brahmastra.sidecar import SidecarStore


class NotFound(Exception):
    pass


class NotAllowed(Exception):
    pass


class _Removed(SidecarStore):
    """Sources a person removed and that must not come back. SOURCE data."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS removed_sources (
        workspace_id TEXT NOT NULL,
        source       TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        title        TEXT,
        removed_by   TEXT,
        removed_at   DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (workspace_id, source, source_id)
    )
    """

    def add(self, source: str, source_id: str, title: str | None, removed_by: str | None) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO removed_sources (workspace_id, source, source_id, title, removed_by, removed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (workspace_id, source, source_id) DO NOTHING"),
                (self.workspace, source, source_id, title, removed_by, time.time()))

    def ids(self, source: str) -> set[str]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT source_id FROM removed_sources WHERE workspace_id = ? AND source = ?"),
                (self.workspace, source))
            return {self._dict(r)["source_id"] for r in cur.fetchall()}


def removed_ids(source: str, workspace: str | None = None) -> set[str]:
    """Ids from `source` (e.g. "notion") that a person removed in this workspace."""
    return _Removed(workspace).ids(source)


def may_remove(identity: Any, owner_id: str | None) -> bool:
    """Admins remove anything; a member removes only what is recorded as theirs."""
    if identity is None:
        return False
    if identity.at_least("admin"):
        return True
    return (identity.at_least("member") and bool(identity.user_id)
            and owner_id is not None and owner_id == identity.user_id)


def who(request: Any) -> tuple[str, str]:
    """(actor, ip) for the audit log, worked out exactly as the account routes do."""
    from brahmastra.routers.auth_routes import _actor, _ip

    return _actor(request), _ip(request)


def refresh_graph(workspace: str) -> dict[str, Any]:
    """
    Rebuild one workspace's graph from the facts left after a removal.

    A removal deletes the facts at once, but the graph built from them (the
    entities, topics and summaries the dashboard and Ask read) changed only on
    the next pipeline run, so a removed meeting stayed on screen as an entity
    until then. This runs the graph-only rebuild: nothing is synced, extracted
    or written back to Notion. Run as a background task, in the workspace the
    request named -- a background task does not inherit the request's binding.
    If another run holds the lock, that run or the next one picks it up: the
    graph was marked stale when the removal happened.
    """
    from brahmastra.pipeline import run_pipeline
    from brahmastra.workspace import reset_request_workspace, set_request_workspace

    token = set_request_workspace(workspace)
    try:
        return run_pipeline(graph_only=True)
    finally:
        reset_request_workspace(token)


def _graph_now_behind(reason: str) -> None:
    from brahmastra.pipeline import mark_dirty

    mark_dirty(reason)


def _audit(actor: str, action: str, target: str, detail: str, ip: str) -> None:
    from brahmastra import accounts

    accounts.store().audit(actor, action, target, detail, ip)


def remove_note(note_id: str, *, actor: str, ip: str = "") -> dict[str, Any]:
    from brahmastra import db, usage
    from brahmastra.accounts import current_identity
    from brahmastra.workspace import current_workspace

    note = db.get_note(note_id)
    if not note:
        raise NotFound("Note not found")
    me = current_identity()
    if not may_remove(me, usage.author_of(note_id)):
        raise NotAllowed("You can remove only notes you added. Ask an admin to remove this one.")

    source = note.get("source") or ""
    title = note.get("title") or note_id
    if source == "notion":
        _Removed().add("notion", note_id, title, me.user_id if me else None)
    db.delete_note(note_id)
    usage.forget_author(note_id)
    _graph_now_behind(f"note {note_id} removed")
    _audit(actor, "note.remove", note_id,
           f"{title} · workspace {current_workspace()}" + (f" · from {source}" if source else ""), ip)
    return {"removed": note_id, "title": title, "kept_out_of_sync": source == "notion"}


def remove_meeting(transcript_id: str, *, purge_notes: bool, actor: str, ip: str = "",
                   store: Any = None) -> dict[str, Any]:
    from brahmastra.accounts import current_identity
    from brahmastra.ingest import assemble
    from brahmastra.ingest.store import get_ingest_store
    from brahmastra.workspace import current_workspace

    store = store or get_ingest_store()
    record = store.get_transcript(transcript_id)
    if record is None:
        raise NotFound(f"no transcript {transcript_id!r}")
    if not may_remove(current_identity(), record.get("submitted_by")):
        raise NotAllowed("You can remove only meetings you uploaded. Ask an admin to remove this one.")

    result = assemble.drop_transcript(transcript_id, store=store, purge_notes=purge_notes)
    _graph_now_behind(f"meeting {transcript_id} removed")
    title = record.get("title") or transcript_id
    _audit(actor, "meeting.remove", transcript_id,
           f"{title} · workspace {current_workspace()} · notes "
           + (f"removed ({result.get('notes_removed', 0)})" if purge_notes else "kept"), ip)
    return result
