"""
The last good graph, kept in Postgres, served (marked stale) when Neo4j is down.

A READ-ONLY FALLBACK, not a second home for derived data. The graph's one home
is the engine; nothing reads this to compute anything, the pipeline never uses
it, and it is never written back. It exists so that a Neo4j outage shows the
team yesterday's graph with a timestamp, rather than an empty screen or --
as happened on 2026-10-13 -- sample notes that looked like data loss.

What is kept, per workspace: exactly what GET /graph and GET /graph/triples
returned the last time the engine answered, plus the stats, the moment the
graph was built and the moment the copy was taken. Losing it loses nothing:
the next refresh writes it again.

REFRESHED twice over, so it cannot silently go stale:
  * after every pipeline run that built a graph (pipeline.py), and
  * by a periodic job in the API process (main.py lifespan), every
    SNAPSHOT_REFRESH_MINUTES (default 30), for every workspace whose graph was
    rebuilt since its copy was taken.
Its age and size are exported as metrics (brahmastra_graph_snapshot_*), so a
refresh job that stops is an alert, not a surprise during an outage.
"""

from __future__ import annotations

import json
import time
from typing import Any

from brahmastra.sidecar import SidecarStore


class SnapshotStore(SidecarStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS graph_snapshots (
        workspace_id TEXT PRIMARY KEY, built_at TEXT, saved_at DOUBLE PRECISION NOT NULL,
        graph TEXT NOT NULL, triples TEXT NOT NULL, stats TEXT NOT NULL, bytes INTEGER NOT NULL)
    """

    def save(self, graph: dict[str, Any], triples: list[dict[str, Any]], stats: dict[str, Any]) -> int:
        self.init_schema()
        g, tr, st = (json.dumps(x, default=str) for x in (graph, triples, stats))
        size = len(g) + len(tr) + len(st)
        with self._cursor() as cur:
            cur.execute(self._ph("DELETE FROM graph_snapshots WHERE workspace_id = ?"), (self.workspace,))
            cur.execute(self._ph(
                "INSERT INTO graph_snapshots (workspace_id, built_at, saved_at, graph, triples, stats, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"),
                (self.workspace, str(graph.get("built_at") or ""), time.time(), g, tr, st, size))
        return size

    def load(self) -> dict[str, Any] | None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph("SELECT * FROM graph_snapshots WHERE workspace_id = ?"), (self.workspace,))
            row = cur.fetchone()
        if not row:
            return None
        row = self._dict(row)
        return {"workspace": self.workspace, "built_at": row["built_at"] or None, "saved_at": row["saved_at"],
                "graph": json.loads(row["graph"]), "triples": json.loads(row["triples"]),
                "stats": json.loads(row["stats"]), "bytes": row["bytes"]}

    def meta(self) -> dict[str, Any] | None:
        """Age and size, without loading the copy (for metrics)."""
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph("SELECT built_at, saved_at, bytes FROM graph_snapshots WHERE workspace_id = ?"),
                        (self.workspace,))
            row = cur.fetchone()
        return self._dict(row) if row else None


def store(workspace: str | None = None) -> SnapshotStore:
    return SnapshotStore(workspace)


def refresh(force: bool = False) -> dict[str, Any]:
    """Copy the bound workspace's current graph, if it changed since the last copy."""
    from brahmastra import db

    graph = db.get_cached_graph()
    if not graph:
        return {"refreshed": False, "reason": "no graph built yet"}
    s = store()
    current = s.meta()
    built = str(graph.get("built_at") or "")
    if not force and current and current.get("built_at") == built and built:
        return {"refreshed": False, "reason": "unchanged"}
    size = s.save(graph, db.get_all_triples(), db.get_db_stats())
    return {"refreshed": True, "bytes": size, "built_at": built}


def refresh_all() -> dict[str, Any]:
    """Every workspace; a failure in one does not stop the others."""
    from brahmastra import db
    from brahmastra.rebuild import bound

    out: dict[str, Any] = {}
    for ws in sorted(w["id"] for w in db.list_workspaces()):
        with bound(ws):
            try:
                out[ws] = refresh()
            except Exception as exc:                            # noqa: BLE001
                out[ws] = {"refreshed": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return out


def stale_payload(snap: dict[str, Any]) -> dict[str, Any]:
    """How a response says it is serving the copy."""
    return {"graph": "unavailable", "served_from": "snapshot", "snapshot_built_at": snap.get("built_at"),
            "snapshot_saved_at": snap.get("saved_at")}
