"""
Graph router — serve the cached concept graph to the frontend.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from brahmastra import db
from brahmastra.graph_health import GraphUnavailable, is_outage

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("")
async def get_graph() -> dict[str, Any]:
    """
    Return the most recently cached graph.

    `availability` says where it came from. When the engine is down this serves
    the last good copy from Postgres (graph_snapshot.py), marked stale with the
    time it was built, so the dashboard can say so instead of showing nothing.
    """
    try:
        cached = db.get_cached_graph()
    except Exception as exc:
        snap = _snapshot_or_raise(exc)
        return {**snap["graph"], "availability": _stale(snap)}
    if cached:
        return {**cached, "availability": {"graph": "ok"}}

    # No graph cached yet — return empty scaffold
    return {
        "built_at": None,
        "graph": {
            "nodes": [],
            "edges": [],
        },
        "availability": {"graph": "ok"},
        "stats": {
            "nodes": 0,
            "edges": 0,
            "central_entities": [],
            "concept_clusters": [],
            "contradictions": [],
            "predicted_links": [],
            "entity_clusters": [],
        },
    }


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    try:
        return db.get_db_stats()
    except Exception as exc:
        snap = _snapshot_or_raise(exc)
        return {**snap["stats"], "availability": _stale(snap)}


@router.get("/triples")
async def get_triples() -> Any:
    try:
        return db.get_all_triples()
    except Exception as exc:
        snap = _snapshot_or_raise(exc)
        # A list has no room for a flag, so the staleness travels in headers.
        return JSONResponse(snap["triples"], headers={
            "X-Brahmastra-Graph": "stale", "X-Brahmastra-Graph-Built-At": str(snap.get("built_at") or "")})


def _stale(snap: dict[str, Any]) -> dict[str, Any]:
    from brahmastra.graph_snapshot import stale_payload

    return stale_payload(snap)


def _snapshot_or_raise(exc: Exception) -> dict[str, Any]:
    """The last good copy when the engine is DOWN; any other error is a bug and propagates."""
    if not is_outage(exc):
        raise exc
    from brahmastra import graph_snapshot

    snap = graph_snapshot.store().load()
    if snap is None:
        raise GraphUnavailable(f"{exc} (and no snapshot has been taken yet)") from exc
    return snap


@router.get("/clusters")
async def get_entity_clusters() -> list[dict[str, Any]]:
    return db.get_entity_clusters()
