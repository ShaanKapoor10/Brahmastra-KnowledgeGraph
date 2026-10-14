"""
Brahmastra backend — FastAPI application.

Routes are registered under /api/* by vercel.json experimentalServices.
FastAPI only sees the path after the prefix (e.g. GET /health).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env so GROQ_API_KEY / ANTHROPIC_API_KEY are available when the
# server is started directly with uvicorn (outside of the MCP process).
from brahmastra.env import load_env

load_env()

from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from brahmastra.auth import auth_status, require_api_key
from brahmastra import logs as _logs

_logs.configure()
from brahmastra.db import init_db
from brahmastra.routers import notes, pipeline, graph, ask, paths, workspaces, entities, diagnostics
from brahmastra.routers import auth_routes
from brahmastra.ingest.routes import router as ingest_router


def _mcp_http_enabled() -> bool:
    return os.environ.get("MCP_HTTP", "").strip().lower() in {"1", "true", "yes"}


# Whether the schema was ever created. Not a cache of "is the store up" -- it
# only records that this work still needs doing, so readiness can finish it.
_schema_ready = False


def _try_init_db() -> str | None:
    """
    Create the schema, and survive not being able to.

    A store that is unreachable at boot must NOT take the process down with it.
    That intent is already written into the health probes -- /health refuses to
    touch the database precisely so a sleeping dependency cannot get the
    container killed and restarted, which cannot fix a dependency -- and the
    lifespan then contradicted it by calling init_db() unguarded.

    That is not hypothetical. Aura Free suspended, its hostname stopped
    resolving, init_schema raised ServiceUnavailable, and uvicorn reported
    "Application startup failed. Exiting." The API was then down for a reason
    that had nothing to do with the API, and it stayed down: a crash loop
    cannot recover, whereas a live process becomes ready the moment the
    dependency returns. Postgres still held every note the whole time.

    Returns None on success, or the reason it could not, for readiness to
    report. The error is not stored: the point is to try again.
    """
    global _schema_ready
    try:
        init_db()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    _schema_ready = True
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialise the store, and run the MCP session manager when serving MCP.

    Mounting a sub-application does NOT run its lifespan — Starlette only runs
    the outermost one. So the mounted MCP app came up with its task group
    uninitialised and every call died on "Task group is not initialized",
    which reads like an MCP bug rather than a wiring mistake. The session
    manager has to be entered here, by the app that actually owns the lifespan.
    """
    # Before anything else: a production install with unsafe settings must not
    # come up at all (brahmastra/preflight.py). No-op outside production.
    from brahmastra import preflight

    preflight.check()
    _try_init_db()
    refresher = asyncio.create_task(_refresh_snapshots_forever())
    try:
        if _mcp_http_enabled():
            from brahmastra.mcp_server import mcp as _mcp

            async with _mcp.session_manager.run():
                yield
        else:
            yield
    finally:
        refresher.cancel()


async def _refresh_snapshots_forever() -> None:
    """
    Keep the stale-graph copy current even if no pipeline runs for a while
    (graph_snapshot.py). Each workspace is copied only when its graph was
    rebuilt since the last copy, so a quiet hour costs one cheap query per
    workspace. Never raises: a failed refresh is visible in the snapshot's age
    metric, and the next tick tries again.
    """
    from brahmastra import graph_snapshot

    interval = max(60.0, float(os.environ.get("SNAPSHOT_REFRESH_MINUTES", "30")) * 60)
    await asyncio.sleep(min(interval, 120))         # let startup settle first
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, graph_snapshot.refresh_all)
        except Exception:                           # noqa: BLE001
            pass
        await asyncio.sleep(interval)


app = FastAPI(
    title="Brahmastra API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # No cross-origin browser access unless CORS_ORIGINS names it. The
    # dashboard never needs CORS: it calls the API from its own server through
    # app/api/[...path]. The old default was "*", which with allow_credentials
    # let any website call the API as whoever was signed in -- failing open
    # again. List origins (comma-separated) only for a browser client that
    # talks to the API directly.
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_headers(request, call_next):
    """Headers every response carries. HSTS belongs to the TLS terminator
    (Caddy), which knows whether the connection really is HTTPS."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# Authentication. Registered after CORS so a rejected request still carries
# CORS headers — otherwise a browser reports an opaque network error instead of
# the 401 that would tell you what is wrong.
app.middleware("http")(require_api_key)

# Header wins over query string: a header is the request's own metadata, while
# a query parameter can be pasted into a link and shared, and a shared link
# quietly writing into someone else's graph is the failure worth avoiding.
# Both are supported because the dashboard's proxy forwards the query string
# untouched, so `?workspace=` works with no plumbing on that side.
WORKSPACE_HEADER = "X-Brahmastra-Workspace"


@app.middleware("http")
async def bind_workspace(request, call_next):
    """
    Resolve the workspace for THIS request, not for the process.

    The API previously read BRAHMASTRA_WORKSPACE once and served that one
    workspace for its whole life. The store and MCP layers had supported many
    graphs for a long time; every HTTP caller could still reach exactly one, so
    the dashboard had no way to show a second and no reason to offer a picker.

    Bound as a ContextVar rather than a parameter threaded through every route,
    so the ~104 db.* call sites keep working unchanged — that indirection is
    what the facade is for. Reset in a finally, because a task that keeps the
    binding hands it to whatever runs next on the same context, and in a system
    whose isolation is "every row carries a workspaceId" that would mean serving
    one graph in place of another.
    """
    from brahmastra.workspace import (
        InvalidWorkspaceId,
        reset_request_workspace,
        set_request_workspace,
        validate_id,
    )

    requested = (
        request.headers.get(WORKSPACE_HEADER)
        or request.query_params.get("workspace")
        or ""
    ).strip()

    if requested:
        try:
            requested = validate_id(requested)
        except InvalidWorkspaceId as e:
            # Refuse rather than fall back to `default`. Silently serving a
            # different graph than the one asked for is how a caller ends up
            # writing into the wrong one and never learning it happened.
            return JSONResponse(
                {"detail": f"invalid workspace: {e}"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    token = set_request_workspace(requested or None)
    from brahmastra.workspace import current_workspace

    request.state.workspace = current_workspace()
    try:
        return await call_next(request)
    finally:
        reset_request_workspace(token)


@app.middleware("http")
async def trace_and_log(request, call_next):
    """
    Outermost: a W3C trace for the request, and one allowlisted log line for it
    (brahmastra/logs.py). The ROUTE TEMPLATE is logged, never the concrete path
    or its query string, and never a body or a header.
    """
    import time as _time

    trace_id, span_id = _logs.start_span(request.headers.get("traceparent"))
    started = _time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["traceparent"] = _logs.traceparent(trace_id, span_id)
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or (
            "/mcp" if request.url.path.startswith("/mcp") else "unmatched")
        elapsed = _time.perf_counter() - started
        try:
            from brahmastra import metrics as _metrics

            _metrics.observe_request(request.method, route, status_code, elapsed)
        except Exception:                                # noqa: BLE001
            pass
        identity = getattr(request.state, "identity", None)
        if route not in ("/health", "/health/ready", "/health/uptime", "/metrics"):
            _logs.request_log.info(
                "request", extra={
                    "event": "request", "method": request.method, "route": route, "status": status_code,
                    "duration_ms": round(elapsed * 1000, 1),
                    "workspace": getattr(request.state, "workspace", None),
                    "user_id": getattr(identity, "user_id", None),
                    "identity": getattr(identity, "kind", None)})

# Remote MCP. The same tools a local stdio server exposes, over HTTP, mounted
# inside this app on purpose: it inherits the auth middleware above, so there
# is ONE authentication boundary rather than a second unprotected door into the
# same graph. Enabled only when asked for, because a local stdio server needs
# nothing of the sort.
if _mcp_http_enabled():
    from brahmastra.mcp_server import mcp as _mcp

    # The sub-app serves at its own `streamable_http_path`, which defaults to
    # "/mcp" — mounting that at "/mcp" puts the endpoint at /mcp/mcp, so a
    # client pointed at /mcp gets a 404 and reports the server as unreachable.
    # Serve it at the mount root instead, so the URL is exactly /mcp.
    _mcp.settings.streamable_http_path = "/"
    app.mount("/mcp", _mcp.streamable_http_app())

@app.exception_handler(Exception)
async def _graph_outage_handler(request, exc):
    """
    A route that needs the graph engine while it is down answers 503 with a
    reason, at once (the circuit breaker in graph_health.py), instead of a 500
    or a request that hangs on a dead socket. Notes, search and /ask do not
    get here: they degrade on their own.
    """
    from brahmastra.graph_health import is_outage

    if is_outage(exc):
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            content={"detail": "The knowledge graph is temporarily unavailable. Notes, "
                                               "search and Ask still work.", "degraded": "graph_unavailable"},
                            headers={"Retry-After": "30"})
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Register routers
app.include_router(auth_routes.router)
app.include_router(notes.router)
app.include_router(pipeline.router)
app.include_router(graph.router)
app.include_router(ask.router)
app.include_router(paths.router)
app.include_router(workspaces.router)
app.include_router(entities.router)
app.include_router(diagnostics.router)
# Transcript ingestion. Mounted here rather than served separately so it
# inherits the auth middleware and the per-request workspace binding: a
# second door into the same data with its own idea of who may open it is
# how isolation gets lost.
app.include_router(ingest_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    """
    Liveness. Answers "is this process running?" and nothing more.

    Deliberately does not touch the database: a liveness probe that fails when
    a dependency is down gets the container killed and restarted, which cannot
    fix a dependency. Restarting a healthy process because Neo4j is asleep just
    turns a degraded system into an unavailable one.
    """
    from brahmastra import version

    # Which code is answering. Cheap -- a hash of the package's source, no
    # database -- so it stays inside the liveness probe. See version.py for the
    # two ways this system has run stale code without anything noticing.
    return {"status": "ok", "service": "brahmastra", "code": version.status()}


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    """
    Prometheus metrics (brahmastra/metrics.py). INTERNAL: open to the scraper on
    the internal network, refused for anything that came through a proxy --
    Caddy blocks the path too, so this is the second layer, not the only one.
    """
    if request.headers.get("x-forwarded-for"):
        return Response(status_code=404)
    import asyncio
    import contextvars

    from brahmastra import metrics as _metrics

    ctx = contextvars.copy_context()
    body, content_type = await asyncio.get_running_loop().run_in_executor(None, ctx.run, _metrics.render)
    return Response(content=body, media_type=content_type)


@app.get("/health/uptime")
async def uptime(response: Response) -> dict[str, str]:
    """
    For an external uptime monitor (UptimeRobot, Better Stack, a status page).

    The public edge exposes this and nothing else from /health: up, degraded or
    down, and no detail. /health/ready names the backends, the auth mode and
    the counts -- useful to an operator, a free reconnaissance report to anyone
    else.

        200 ok            both stores answer
        200 degraded      the graph engine is down; notes, search and Ask work
        503 unavailable   the system of record (Postgres) cannot be reached
    """
    components = await _components()
    state = _overall(components)
    if state == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": state}


@app.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    """
    Readiness: can this instance serve traffic?

    Ready means the SYSTEM OF RECORD answers. The graph engine is derived and
    may be down -- that is "degraded", still 200, because notes, note search
    and Ask keep working and the dashboard shows the last good graph. Failing
    readiness on a Neo4j outage would make every orchestrator and proxy drop
    the whole service for the loss of one rebuildable cache; it is how a
    suspended Aura once blanked the dashboard. 503 only when Postgres is down.
    """
    from brahmastra.graph_health import breaker
    from brahmastra.stores import backend_name, note_backend_name

    notes_on = note_backend_name() or backend_name()
    detail: dict[str, Any] = {
        "service": "brahmastra",
        "backend": backend_name(),
        "system_of_record": notes_on,
        # "unconfigured" means the API is refusing all traffic, and
        # "anonymous" means it is open to anyone who can reach it.
        "auth": auth_status(),
    }
    if notes_on == "sqlite":
        detail["warnings"] = [
            "system of record is SQLite: a single file on this container's "
            "disk, discarded on redeploy unless it is on a volume, unreadable "
            "by any other instance, and lexical-only (no hybrid search). Set "
            "NOTE_BACKEND=postgres for anything deployed."
        ]
    components = await _components(detail)
    detail["components"] = components
    detail["graph_breaker"] = breaker.snapshot()
    detail["status"] = {"ok": "ready", "degraded": "degraded", "unavailable": "unavailable"}[_overall(components)]
    if detail["status"] == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return detail


def _overall(components: dict[str, str]) -> str:
    if components.get("notes") != "ok":
        return "unavailable"
    return "ok" if components.get("graph") == "ok" else "degraded"


async def _components(detail: dict[str, Any] | None = None) -> dict[str, str]:
    """Probe each store separately, bounded, off the event loop."""
    import asyncio

    def probe() -> dict[str, str]:
        from brahmastra import db

        out: dict[str, str] = {}
        # Finish what startup could not, FIRST: the schema is created
        # idempotently, so an instance that booted while a store was down
        # becomes ready on its own the moment the store returns -- and the
        # notes check below needs the tables to exist.
        init_failure = _try_init_db() if not _schema_ready else None
        try:
            db.list_workspaces()                 # a SOURCE method: answered by the note store
            out["notes"] = "ok"
        except Exception as exc:                 # noqa: BLE001
            out["notes"] = f"unavailable: {type(exc).__name__}: {exc}"[:200]
        try:
            if init_failure is not None:
                raise RuntimeError(init_failure)
            stats = db.get_db_stats()
            out["graph"] = "ok"
            if detail is not None:
                detail.update(workspace=stats.get("workspace"), notes=stats.get("notes_total"),
                              graph_cached=stats.get("graph_cached"))
        except Exception as exc:                 # noqa: BLE001
            out["graph"] = f"unavailable: {type(exc).__name__}: {exc}"[:200]
        return out

    # run_in_executor does NOT carry context variables into the thread, and the
    # request's workspace is one (main.bind_workspace): without copying the
    # context, readiness reported `default` whatever workspace was asked for.
    import contextvars

    ctx = contextvars.copy_context()
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, ctx.run, probe), timeout=10)
    except Exception:                            # noqa: BLE001
        return {"notes": "unavailable: timeout", "graph": "unavailable: timeout"}
