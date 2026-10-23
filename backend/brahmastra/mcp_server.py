"""
MCP server — Brahmastra as memory for any agent that speaks MCP.

Run:
  cd backend && python -m brahmastra.mcp_server          (stdio, one user)
  MCP_HTTP=1 on the API server                            (remote, /mcp, per user)

What an agent is given, in the order it meets them:
  instructions   sent on connect; the loop and which tool to use (INSTRUCTIONS)
  tools          14, each labelled read-only or not (tests/test_mcp_server.py)
  prompts        recall, remember, onboard -- slash commands in Claude Code
  resource       brahmastra://guide -- the long form of the instructions

Brahmastra is a TOOL inside someone else's project. Four layers, each with one
owner, and this file speaks only for the first:
  1. the tool       how to use the memory -- here, identical in every project
  2. the project    its CLAUDE.md / AGENTS.md -- its own, and it wins a conflict
  3. the workspace  the `briefing` note -- a team's shared context that no repo
                    file holds (scope, owners, agreements, current focus)
  4. the person     their own client settings, never shared
`brahmastra_get_briefing` returns layer 3. It adds to a project's instructions
and never replaces or overrides them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from brahmastra.env import load_env

load_env()

# Pin the noisiest loggers BEFORE anything can reconfigure them.
#
# Setting a level on the logger itself, rather than lowering the root, is the
# point: torch and transformers arrive later in the import chain, one of them
# calls logging.basicConfig, and the root logger comes back at INFO with a
# RichHandler attached. Anything left at NOTSET inherits that and starts
# talking. An explicit level here survives it.
for _noisy in ("neo4j", "neo4j.notifications", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from brahmastra import db

# Sent to every client in the MCP `initialize` response, so any agent that
# connects — Claude Code, Claude Desktop, Cursor, anything speaking MCP — knows
# this memory exists without being told again per session.
#
# This was empty for a long time, and that was the whole reason the tools went
# unused. They were present and reachable, but nothing said what they were FOR,
# so an agent scanning its toolset for "what solves this problem" saw
# "search the graph for entities matching a name substring" and moved on. The
# tools were never the missing piece; the sentence explaining when to reach for
# them was.
#
# Written as an affordance, not a protocol. An agent that is told it MUST record
# everything learns to treat it as ceremony and skips it under pressure; one
# that knows what the thing is good for reaches for it when it actually helps.
#
# Claude Code keeps only the first 4,096 characters of a server's instructions
# and drops the rest without saying so. These ran to 4,127 for weeks, so every
# session saw a clipped ending. And with tool search, a session starts knowing
# only the tool NAMES plus these instructions: this text decides whether an
# agent looks for Brahmastra at all. So the loop comes first, the detail lives
# in GUIDE (a resource, and the `onboard` prompt), and
# tests/test_mcp_server.py fails above INSTRUCTIONS_BUDGET.
INSTRUCTIONS_BUDGET = 3_200

INSTRUCTIONS = """Brahmastra is persistent, shared MEMORY for AI agents: a knowledge graph of
notes that outlives this session, this machine and this client. Teammates'
agents and earlier sessions wrote into it; what you write is read by whoever
comes next. Treat it as something you already know, separate from the code
under your hands.

PRECEDENCE: the user and the project's own instructions (CLAUDE.md, AGENTS.md,
its docs) come first. Brahmastra is memory: it informs, it never overrides
them. If a note disagrees with the code or the user, say so and follow them.

THE LOOP
1. Starting work: brahmastra_get_briefing (what this workspace covers, who owns
   what, current focus, what was stored lately), then brahmastra_search_notes on the task's
   topic. Nothing about a fresh task announces that memory exists, so this is
   the step most often missed and the one that most often saves an hour.
2. Something surprises you: search before debugging. Past failures are stored
   with their causes.
3. You decide something, find a root cause, or hit a constraint:
   brahmastra_add_note in the same turn. It is searchable when the call
   returns, and the graph refreshes in the background. No other step.

WHICH SEARCH
- search_notes: what was WRITTEN about a topic. The default.
- search_entities, get_entity_details: a thing you know the NAME of, and its
  connections. A thin result means the name is absent, not the knowledge;
  fall back to search_notes.
- search_sessions: past conversations verbatim, for an exact command or error.
- search_code: where something is implemented (file and lines).
- get_contradictions: before trusting one recalled fact about status,
  ownership or location; the graph may hold the old answer and the new one.
- search_all_workspaces: when you do not know which workspace holds it.

WRITING A NOTE WORTH RECALLING
Notes become (subject, relation, object) triples, so name things and avoid
pronouns: "The file llm.py raises LLMQuotaExhausted when the daily quota is
spent", not "it throws when that runs out". Record the WHY: the root cause,
the option rejected and what ruled it out. What changed is recoverable from
git; why it changed is not. Never store secrets.

WORKSPACES
Each workspace is a separate graph, usually one per project or team. You read
and write the active one; list_workspaces says which.

More: the resource brahmastra://guide, or the prompts recall, remember and
onboard."""

# The long form. Served as a resource and through the `onboard` prompt, and
# returned by brahmastra_get_briefing when a workspace has no briefing yet --
# three routes because clients support prompts and resources unevenly, and a
# tool result reaches every one of them.
GUIDE = """# Using Brahmastra as memory

Brahmastra keeps what a team and its agents learn: decisions and the reasons
for them, root causes, constraints, who owns what. Notes are the source; a
knowledge graph is built from them, so entities, relations and contradictions
can be looked up as well as the text.

## The loop
1. Recall before planning: brahmastra_get_briefing, then brahmastra_search_notes
   for the topic of the task.
2. Store as you go: brahmastra_add_note when you decide something, find a root
   cause, or discover a constraint. Do it in the same turn; batching to the end
   of a session is how things go missing.
3. Check before trusting: brahmastra_get_contradictions for facts about status,
   ownership, reporting lines or location.

## Tools
| tool | use it to |
|---|---|
| get_briefing | start on a workspace: its purpose, briefing note, recent notes |
| search_notes | recall what was written about a topic (hybrid: words and meaning) |
| search_entities | find a named thing and its connections |
| get_entity_details | one entity: relations, aliases, centrality, where code is defined |
| search_sessions | the raw past conversations, verbatim |
| search_code | where something is implemented in the indexed repository |
| get_contradictions | facts recorded once and contradicted later |
| get_graph_stats | how big the memory is and what it is mostly about |
| add_note | remember something; extracted at once |
| run_pipeline | rebuild the graph; rarely needed, see below |
| list_workspaces | which graphs exist and which is active |
| create_workspace | a new, fully separate graph (admins) |
| search_all_workspaces | search every workspace you may open |
| recall | for client hooks: recalled notes shaped as a hook result |

## Writing notes
- One topic per note, with a title a stranger would search for.
- Name files, functions, services, people and teams explicitly. Pronouns do
  not extract into the graph.
- Lead with the why: root cause, rejected options and what ruled them out,
  the constraint found the hard way.
- Dates as YYYY-MM-DD. Never secrets, keys or passwords.

## Where Brahmastra sits
Brahmastra is a tool inside your project, not its rulebook. The user and the
project's own files (CLAUDE.md, AGENTS.md, its docs) come first; memory
informs and never overrides them. When a note disagrees with the code or the
user, say so and follow the code and the user -- then store the correction.

## The briefing note
Each workspace can hold one note with the id `briefing`, written by the team
that uses the workspace. brahmastra_get_briefing returns it first. It holds
the shared context that lives in no repository file:
- what the workspace covers (often several repositories, or none);
- who owns what, and who to ask;
- the team's working agreements;
- the current focus, and what is deliberately out of scope;
- which repository files hold the rules for the code (point to them; do not
  copy them).
It does NOT hold build commands, code conventions or repository traps: those
belong in that repository's own CLAUDE.md or AGENTS.md, which already load.
Write or replace it with
brahmastra_add_note(note_id="briefing", title="<workspace> briefing", content=...).
A project needs nothing about Brahmastra in its own files.

## Rebuilding
brahmastra_add_note extracts the note immediately and refreshes the graph in
the background. brahmastra_run_pipeline is for after a bulk import or an
ontology change; it starts a run and returns at once, and get_graph_stats
reports when it has finished.
"""

BRIEFING_NOTE_ID = "briefing"
RECENT_DAYS = 30

mcp = FastMCP("brahmastra", instructions=INSTRUCTIONS)

# What each tool does to the world, declared rather than left for a client to
# guess from the name. A client can then run reads without asking and confirm
# writes, which is what lets an agent recall freely.
READS = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                        idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                         idempotentHint=False, openWorldHint=False)
# A pipeline run can write insights back to the workspace's Notion pages.
REBUILDS = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                           idempotentHint=True, openWorldHint=True)


# ---------------------------------------------------------------------------
# Who is calling: every tool is scoped to the caller when served over HTTP
# ---------------------------------------------------------------------------
#
# Over stdio the server is a local process started by its own user, and the
# workspace comes from BRAHMASTRA_WORKSPACE, exactly as before. Over HTTP
# (MCP_HTTP=1, mounted at /mcp) many people share one server, so each call is
# re-identified from ITS OWN request -- the token, and the workspace header --
# and checked here, inside the tool. The API middleware's bindings are not
# relied on: tools run in the MCP session manager's task group, where they
# are not guaranteed to arrive, which is why remote MCP used to be admin-only.

WORKSPACE_HEADER = "X-Brahmastra-Workspace"


def _http_request():
    """The HTTP request behind this tool call, or None over stdio."""
    try:
        return mcp.get_context().request_context.request
    except (LookupError, ValueError, AttributeError):
        return None


def _denied(detail: str) -> str:
    return json.dumps({"error": detail}, indent=2)


def _scoped(need: str):
    """Require `need` (viewer | member | admin), the plan features the tool needs
    (plans.TOOL_FEATURES), and membership of the workspace."""
    import functools

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            request = _http_request()
            if request is None:
                return fn(*args, **kwargs)

            from brahmastra.accounts import bind_identity, unbind_identity
            from brahmastra.auth import resolve_identity
            from brahmastra.workspace import (
                InvalidWorkspaceId,
                current_workspace,
                reset_request_workspace,
                set_request_workspace,
                validate_id,
            )

            identity, problem = resolve_identity(request)
            if identity is None:
                return _denied(problem[1])
            if not identity.at_least(need):
                return _denied(f"This tool needs the {need} role; you are a {identity.role}.")
            # Checked per call, not only on connecting: a plan changed mid-session
            # applies at the next call, as a revoked token does.
            from brahmastra.plans import TOOL_FEATURES, refusal

            for feature in ("mcp", TOOL_FEATURES.get(fn.__name__)):
                if feature and not identity.has(feature):
                    return _denied(refusal(feature))
            requested = (request.headers.get(WORKSPACE_HEADER)
                         or request.query_params.get("workspace") or "").strip()
            try:
                workspace = validate_id(requested) if requested else current_workspace()
            except InvalidWorkspaceId as e:
                return _denied(f"invalid workspace: {e}")
            if not identity.can_open(workspace):
                return _denied(f"You are not a member of the workspace '{workspace}'.")

            ws_token = set_request_workspace(workspace)
            id_token = bind_identity(identity)
            try:
                return fn(*args, **kwargs)
            finally:
                unbind_identity(id_token)
                reset_request_workspace(ws_token)
        return wrapper
    return decorate


def allowed_hosts_and_origins(env: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Host and Origin values remote MCP accepts (DNS-rebinding protection).

    The SDK's default accepts only localhost, so behind Caddy -- where the Host
    header is the install's domain -- every remote MCP request was refused with
    421, and nobody saw it because remote MCP had only been tried locally. The
    protection stays on; it is told this install's own name instead:
    MCP_ALLOWED_HOSTS if set (comma-separated), else the host of PUBLIC_URL and
    DOMAIN, plus localhost for a server reached from its own machine.
    """
    from urllib.parse import urlparse

    env = dict(os.environ if env is None else env)
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    origins: list[str] = []
    explicit = [h.strip() for h in env.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    public = env.get("PUBLIC_URL", "").strip()
    if public:
        origins.append(public.rstrip("/"))
        if urlparse(public).hostname:
            hosts.append(urlparse(public).hostname or "")
    if env.get("DOMAIN", "").strip():
        hosts.append(env["DOMAIN"].strip())
    hosts += explicit
    for origin in env.get("CORS_ORIGINS", "").split(","):
        if origin.strip() and origin.strip() != "*":
            origins.append(origin.strip().rstrip("/"))
    hosts = list(dict.fromkeys(h.lower() for h in hosts if h))
    # "name:*" admits the name on any port (a trial install on :8443).
    return [*hosts, *(f"{h}:*" for h in hosts)], list(dict.fromkeys(origins))


def _visible(workspace_id: str | None) -> bool:
    """Whether the caller may see a workspace (always, over stdio)."""
    from brahmastra.accounts import current_identity

    identity = current_identity()
    return identity is None or identity.can_open(workspace_id or "")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=REBUILDS)
@_scoped("member")
def brahmastra_run_pipeline(mode: str = "incremental", wait: bool = False) -> str:
    """
    Rebuild the graph: sync sources, extract pending notes, resolve entities,
    build, summarise. Rarely needed -- brahmastra_add_note already extracts its
    note and refreshes the graph. Use after a bulk import or an ontology change.

    Starts the run and returns at once; brahmastra_get_graph_stats reports
    `pipeline.running` and the last run's result. wait=True blocks until done
    (about a minute; the server answers nothing else meanwhile).
    mode: 'incremental' (pending notes only) or 'full' (re-extract everything).
    """
    db.init_db()
    from brahmastra import pipeline

    if wait:
        return json.dumps(pipeline.run_pipeline(full=(mode == "full")), indent=2)
    started = pipeline.run_in_background(full=(mode == "full"))
    return json.dumps({**started, "check": "brahmastra_get_graph_stats -> pipeline"}, indent=2)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_get_graph_stats() -> str:
    """
    See what this memory contains before relying on it — or on finding it empty.

    Node and edge counts, top entities by PageRank, cluster count. Useful as a
    first orientation on a returning project: the top entities are a fair
    summary of what the graph is actually about.
    """
    db.init_db()
    cached = db.get_cached_graph()
    stats = db.get_db_stats()
    payload: dict[str, Any] = {"db": stats}
    if cached:
        payload["built_at"] = cached["built_at"]
        payload["graph"] = {
            "nodes": cached["stats"].get("nodes"),
            "edges": cached["stats"].get("edges"),
            "concept_clusters": len(cached["stats"].get("concept_clusters", [])),
            "contradictions": len(cached["stats"].get("contradictions", [])),
            "predicted_links": len(cached["stats"].get("predicted_links", [])),
            "top_entities": cached["stats"].get("central_entities", [])[:5],
        }
    else:
        payload["graph"] = None
    payload["pipeline"] = _pipeline_state()
    return json.dumps(payload, indent=2)


def _pipeline_state() -> dict[str, Any]:
    """Whether a rebuild is running, how the last one ended, and whether the
    graph is behind the notes -- what a caller of a background run checks."""
    from brahmastra import pipeline

    state = pipeline.run_state()
    last = state.get("last") or {}
    return {
        "running": state["running"],
        "graph_behind_notes": state["stale"],
        "last_run": {k: last.get(k) for k in ("status", "mode", "finished_at", "failed_stages")}
                    if last else None,
    }


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_search_entities(query: str = "", entity_type: str = "", limit: int = 10) -> str:
    """
    Find a NAMED thing in the graph — a person, file, service, project.

    Matches entity names only, so use it when you know what something is called
    and want its connections. To recall what was WRITTEN about a subject, use
    brahmastra_search_notes instead: an empty result here means the name is
    absent, never that the knowledge is.

    entity_type options: person, project, concept, tool, organisation, event, date, unknown.
    """
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."

    nodes = cached["graph"].get("nodes", [])
    results = []
    for n in nodes:
        if query and query.lower() not in n["label"].lower():
            continue
        if entity_type and n.get("type", "").lower() != entity_type.lower():
            continue
        results.append(n)

    results.sort(key=lambda x: x.get("pagerank", 0), reverse=True)
    return json.dumps(results[:limit], indent=2)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_search_notes(query: str, limit: int = 10) -> str:
    """
    RECALL what is already known about a topic. Start here on unfamiliar work.

    Searches the full text of every stored note, so it finds decisions, root
    causes, rejected options and past breakages — including from sessions you
    had no part in. On a backend that supports it this is hybrid search, so a
    note phrased differently from your query still surfaces.

    Prefer this over brahmastra_search_entities, which matches entity NAMES only
    and comes up thin whenever the name happens to differ from your wording.
    For the verbatim conversation a note was distilled from, use
    brahmastra_search_sessions.
    """
    db.init_db()
    notes = db.search_notes(query, limit=limit)
    if not notes:
        return f"No notes matching '{query}'."
    out = []
    for n in notes:
        content = n.get("content", "")
        snippet = content if len(content) <= 400 else content[:400] + "…"
        out.append({
            "id": n["id"],
            "title": n["title"],
            "status": n.get("extraction_status"),
            "snippet": snippet,
        })
    return json.dumps(out, indent=2, ensure_ascii=False)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_search_sessions(query: str, limit: int = 8) -> str:
    """
    RECALL from the raw conversations of past coding sessions, verbatim.

    Notes are distilled from sessions by a model, which keeps only the end of
    a long stretch and can drop or misstate a detail. This searches the
    conversation itself -- each request with the work that answered it -- so
    "how did we fix the Notion leak" finds the exchange where it happened, in
    the words used at the time. Secrets are redacted at indexing.

    Use it when brahmastra_search_notes comes up thin, or when you need the
    exact reasoning, command or error rather than a summary of it.
    """
    from brahmastra import sessions

    hits = sessions.search(query, limit=limit)
    if not hits:
        return (f"No session exchanges matching '{query}'. If none are indexed "
                "yet: python -m brahmastra.sessions --backfill <claude project dir>")
    for h in hits:
        if len(h["text"]) > 700:
            h["text"] = h["text"][:700] + "…"
    return json.dumps(hits, indent=2, ensure_ascii=False)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_search_code(query: str, scope: str = "source", limit: int = 6) -> str:
    """
    FIND where something is implemented in this repository -- file and lines.

    Searches the tracked source files, cut at function/class/heading
    boundaries, by meaning and by words. Notes say what was decided about the
    code; this says where it is. `scope`: source (default), tests, docs, all.
    """
    from brahmastra import code_index

    if scope not in code_index.SCOPES:
        return f"scope must be one of {', '.join(code_index.SCOPES)}"
    hits = code_index.search(query, limit=limit, scope=scope)
    if not hits:
        return (f"No code matching '{query}'. If nothing is indexed yet: "
                "python -m brahmastra.code_index --index")
    for h in hits:
        if len(h["text"]) > 900:
            h["text"] = h["text"][:900] + "…"
    return json.dumps(hits, indent=2, ensure_ascii=False)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_get_entity_details(entity_name: str) -> str:
    """Return full details for a named entity: aliases, PageRank, cluster, and all relations."""
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."

    nodes = cached["graph"].get("nodes", [])
    node = next((n for n in nodes if n["label"].lower() == entity_name.lower()), None)
    if not node:
        node = next((n for n in nodes if entity_name.lower() in n["label"].lower()), None)
    if not node:
        return f"Entity '{entity_name}' not found in graph."

    canonical = node["label"]
    clusters = db.get_entity_clusters()
    aliases: list[str] = []
    for c in clusters:
        if c["canonical_name"].lower() == canonical.lower():
            aliases = [m for m in c["mentions"] if m != canonical]
            break

    edges = cached["graph"].get("edges", [])
    detail = {
        "entity": canonical,
        "type": node.get("type"),
        "pagerank": node.get("pagerank"),
        "cluster": node.get("cluster"),
        "aliases": aliases,
        "outgoing_relations": [e for e in edges if e["source"] == canonical],
        "incoming_relations": [e for e in edges if e["target"] == canonical],
    }
    # The graph knows `_groq_chat` as a name; the code index knows where it
    # lives. Joined on the exact identifier, so a hit is a definition, not a
    # mention -- and only for names spelled like code, which is the same
    # syntactic test that typed it code_symbol in the first place.
    try:
        from brahmastra import code_index
        from brahmastra.ontology import code_symbol_type

        names = [canonical] + aliases
        if any(code_symbol_type(n, "unknown") for n in names):
            defined = code_index.where_defined(names)
            if defined:
                detail["defined_at"] = defined
    except Exception:                                   # noqa: BLE001
        pass            # the code index is an extra; never fail the lookup on it
    return json.dumps(detail, indent=2)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_get_contradictions() -> str:
    """
    Facts that conflict — where something recorded once was later contradicted.

    Worth checking before trusting a single recalled fact about a functional
    relation (who reports to whom, what status something has, where it lives):
    the graph may hold both the old answer and the new one.
    """
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."
    return json.dumps(cached["stats"].get("contradictions", []), indent=2)


@mcp.tool(annotations=WRITES)
@_scoped("member")
def brahmastra_add_note(
    title: str, content: str, note_id: str = "", publish: bool = False
) -> str:
    """
    REMEMBER something durable — a root cause, a decision and its reasoning,
    a constraint found the hard way.

    Write plain entity-rich sentences ("The file llm.py raises
    LLMQuotaExhausted", not "it throws when the quota runs out"); that is what
    extracts into a useful graph. Record the WHY above all: what changed is
    usually recoverable from the code later, and why it changed is not.

    The note is searchable when this returns — extraction runs here rather than
    waiting for a pipeline. That matters more than it sounds: while storing took
    a call PLUS a separate pipeline run, the second step was routinely skipped
    and the note sat `pending`, which means invisible to every search and to
    every other session.

    publish=True also gives the note a page in the workspace's Notion database,
    created on the next write-back. Use it for prose a human will want to
    re-read — decisions, design records. Leave it off for working memory such
    as session checkpoints, which belong in the graph, not in Notion.
    """
    db.init_db()
    from brahmastra import usage
    from brahmastra.accounts import current_identity

    # Counted as for the dashboard: an assistant adding notes in a loop meets
    # the same daily limit a person would.
    try:
        usage.charge_note(current_identity(), content)
    except usage.LimitReached as e:
        return _denied(str(e))
    nid = note_id or str(uuid.uuid4())[:8]
    # Stamped, so "what was stored lately" (brahmastra_get_briefing) has an
    # answer; notes stored here used to carry no time at all.
    from datetime import datetime, timezone
    db.upsert_note(nid, title, content, last_edited=datetime.now(timezone.utc).isoformat(),
                   mark_pending=True, publish=publish or None, source="mcp")

    # Extract immediately, and treat failure as a delay rather than an error.
    # A rate limit or a dead provider leaves the note `pending`, which is
    # exactly the old behaviour: the next pipeline run picks it up. So this can
    # only improve on what happened before, never lose the note.
    extracted: dict[str, Any] = {"extraction": "pending"}
    try:
        from brahmastra.extraction import extract_note
        result = extract_note(db.get_note(nid) or {})
        if result.get("error"):
            extracted = {"extraction": "pending", "reason": str(result["error"])[:200]}
        else:
            extracted = {"extraction": "done", "triples": result.get("triples_added", 0)}
    except Exception as e:                       # noqa: BLE001
        extracted = {"extraction": "pending", "reason": f"{type(e).__name__}: {e}"[:200]}

    return json.dumps({"status": "added", "note_id": nid, "title": title,
                       "publish": bool(publish), **extracted,
                       "graph": _refresh_graph_soon()}, indent=2)


def _refresh_graph_soon() -> str:
    """Bring the graph up to date with the note just stored, without the agent
    having to remember a second call. The note's facts are stored already; the
    entities, merges and topics built from them are not, until a rebuild. A
    graph-only rebuild (no sync, no model calls) on a thread, after a short
    pause so a burst of notes shares one. GRAPH_AUTO_REFRESH=0 turns it off."""
    if os.environ.get("GRAPH_AUTO_REFRESH", "1").strip() == "0":
        return "not refreshed (GRAPH_AUTO_REFRESH=0); run brahmastra_run_pipeline"
    from brahmastra import pipeline

    started = pipeline.run_in_background(
        graph_only=True, delay_s=float(os.environ.get("GRAPH_REFRESH_DELAY_S", "15")))
    return ("refreshing in the background" if started["started"]
            else "a rebuild already scheduled or running will include it")


# ---------------------------------------------------------------------------
# Workspaces — several independent graphs
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_list_workspaces() -> str:
    """
    List the knowledge graphs available, and which one is currently active.

    Each workspace is a fully separate graph: notes, entities and relations in
    one are invisible to the others. Use this before adding a note to confirm
    which graph it will land in.
    """
    db.init_db()
    return json.dumps({
        "current": db.workspace(),
        "workspaces": [w for w in db.list_workspaces() if _visible(w.get("id"))],
    }, indent=2)


@mcp.tool(annotations=WRITES)
@_scoped("admin")
def brahmastra_create_workspace(
    name: str,
    id: str = "",
    description: str = "",
    notion_database_id: str = "",
) -> str:
    """
    Create a new, empty knowledge graph.

    Use for a separate area of knowledge that should not mix with the current
    one — work versus personal, or a single project. The new workspace starts
    empty and inherits nothing.

    name: display name, e.g. "Office"
    id:   optional slug; derived from name when omitted
    description: what this graph is for
    notion_database_id: this workspace's own Notion source (optional)
    """
    from brahmastra.workspace import InvalidWorkspaceId, slugify
    db.init_db()
    try:
        created = db.create_workspace(
            id=id or slugify(name),
            name=name,
            description=description,
            notion_database_id=notion_database_id or None,
        )
    except InvalidWorkspaceId as e:
        return json.dumps({"error": f"invalid workspace id: {e}"}, indent=2)
    except NotImplementedError as e:
        return json.dumps({"error": str(e)}, indent=2)
    return json.dumps({
        "created": created,
        # Creating does not switch: an agent should not silently redirect
        # where the user's next note gets written.
        "note": (
            f"Created but NOT switched to. The active workspace is still "
            f"{db.workspace()!r}. Set BRAHMASTRA_WORKSPACE={created['id']} to "
            f"make it active, or pass the workspace explicitly."
        ),
    }, indent=2)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_search_all_workspaces(query: str, limit: int = 10) -> str:
    """
    Search the full text of notes across EVERY workspace.

    Normal search only sees the active graph. Use this when you do not know
    which workspace holds something. Each result reports the workspace it came
    from.
    """
    db.init_db()
    try:
        from brahmastra.accounts import current_identity

        identity = current_identity()
        # Only the workspaces this caller may open; None (every one) only for
        # callers who may open every one, or over stdio.
        allowed = None if identity is None or identity.workspaces is None else sorted(identity.workspaces)
        hits = db.search_notes_across(query, allowed, limit) if allowed != [] else []
    except NotImplementedError as e:
        return json.dumps({"error": str(e)}, indent=2)
    return json.dumps({
        "query": query,
        "results": [
            {
                "workspace": h.get("workspace_id"),
                "id": h.get("id"),
                "title": h.get("title"),
                "snippet": (h.get("content") or "")[:280],
            }
            for h in hits
        ],
    }, indent=2)


# ---------------------------------------------------------------------------
# Starting on a project
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_get_briefing() -> str:
    """
    START HERE on a project: the workspace's briefing note (what it covers,
    who owns what, team agreements, current focus), the notes stored in the
    last 30 days, and how much the memory holds. When the workspace has no
    briefing yet, says how to write one.

    The briefing adds the team's shared context to the project's own
    CLAUDE.md / AGENTS.md; those, and the user, take precedence over it.
    """
    db.init_db()
    workspace_id = db.workspace()
    workspace = db.get_workspace(workspace_id) or {}
    briefing = db.get_note(BRIEFING_NOTE_ID)
    notes = [n for n in db.get_notes() if n.get("id") != BRIEFING_NOTE_ID]
    # Recent means recent. Notes stored over MCP before 2026-10-22 carry no
    # time, so without a cutoff the "latest" were months-old Notion pages.
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()
    dated = sorted((n for n in notes if (n.get("last_edited") or "") >= since),
                   key=lambda n: n["last_edited"], reverse=True)
    cached = db.get_cached_graph()
    payload: dict[str, Any] = {
        "workspace": {"id": workspace_id, "name": workspace.get("name"),
                      "description": workspace.get("description")},
        "briefing": ({"title": briefing.get("title"), "content": briefing.get("content")}
                     if briefing else None),
        "recent_notes": [{"id": n["id"], "title": n.get("title"), "edited": n["last_edited"]}
                         for n in dated[:8]],
        "memory": {
            "notes": len(notes) + (1 if briefing else 0),
            "top_entities": [e.get("entity") if isinstance(e, dict) else e
                             for e in (cached or {}).get("stats", {}).get("central_entities", [])[:8]],
            "graph_built_at": (cached or {}).get("built_at"),
        },
        "pipeline": _pipeline_state(),
    }
    if not briefing:
        payload["write_a_briefing"] = (
            f"This workspace has no briefing. If the user wants one, ask what the "
            f"workspace covers, who owns what, the team's agreements and current focus "
            f"(not build commands or code rules: those belong in the repository's own "
            f"CLAUDE.md or AGENTS.md), then store it with brahmastra_add_note("
            f"note_id='{BRIEFING_NOTE_ID}', title='<workspace> briefing', content=...).")
        payload["guide"] = GUIDE
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


@mcp.tool(annotations=READS)
@_scoped("viewer")
def brahmastra_recall(prompt: str = "", event: str = "UserPromptSubmit", active: str = "",
                      session: str = "") -> str:
    """
    For client hooks, not for agents: recalled memory shaped as a Claude Code
    hook result. Agents use brahmastra_search_notes and brahmastra_get_briefing.

    event=UserPromptSubmit: the notes matching `prompt`, plus the workspace
    briefing on a session's first request.
    event=SessionStart: the workspace briefing, after a compaction.
    event=Stop: `prompt` is the agent's last message; a one-line reminder to
    store what it settled, once per turn (`active` is stop_hook_active).
    `session` lets the Stop check also read the request that started the turn.
    """
    # Why a separate tool: Claude Code hands an MCP hook's result to the
    # model only when it is JSON with hookSpecificOutput.additionalContext.
    # Plain text -- what every other tool returns -- reaches it as the bare
    # word "completed". Measured 2026-10-23: the plugin's recall hook ran,
    # fetched the right notes, and the agent never saw them. The rules live in
    # agent_hooks, shared with the HTTP hook route other clients use.
    from brahmastra import agent_hooks

    payload: dict[str, Any] = {"hook_event_name": event, "session_id": session,
                               "stop_hook_active": active}
    payload["last_assistant_message" if event == "Stop" else "prompt"] = prompt
    if event == "SessionStart":
        payload["source"] = "compact"
    out = agent_hooks.handle("claude", payload)
    return json.dumps(out, ensure_ascii=False) if out else ""


# Prompts appear as slash commands (/brahmastra:recall in Claude Code) and the
# resource can be @-mentioned. Static text, so nothing here needs scoping: the
# tools they lead to are scoped when called.

@mcp.prompt(name="recall", title="Recall before starting",
            description="Load what Brahmastra already knows about a task before planning it.")
def recall_prompt(topic: str) -> str:
    return (
        f"Before planning anything, recall what is already known about: {topic}\n\n"
        "1. Call brahmastra_get_briefing for the workspace's purpose and rules.\n"
        "2. Call brahmastra_search_notes with the topic, then once or twice more "
        "with different wording.\n"
        "3. For anything you know the name of, call brahmastra_get_entity_details.\n"
        "4. If a recalled fact is about status, ownership or location, check "
        "brahmastra_get_contradictions.\n\n"
        "Then summarise what was decided, what was tried and failed, and what is "
        "open, citing note ids. If nothing is stored, say so plainly.")


@mcp.prompt(name="remember", title="Remember this",
            description="Store decisions, root causes and constraints from this session in Brahmastra.")
def remember_prompt(what: str = "") -> str:
    target = (f"Store this in Brahmastra: {what}" if what.strip() else
              "Review this conversation for decisions, root causes, rejected options and "
              "constraints that are not stored in Brahmastra yet, and store each one.")
    return (
        f"{target}\n\n"
        "Use brahmastra_add_note, one note per topic:\n"
        "- a title a stranger would search for;\n"
        "- plain sentences naming files, functions, services and people (no pronouns);\n"
        "- the WHY first: root cause, options rejected and what ruled them out;\n"
        "- dates as YYYY-MM-DD; never secrets, keys or passwords.\n"
        "Search first (brahmastra_search_notes) so you update an existing note "
        "(same note_id) instead of duplicating it.")


@mcp.prompt(name="onboard", title="Onboard to this project",
            description="Learn how to use Brahmastra, then read this workspace's briefing.")
def onboard_prompt() -> str:
    return (GUIDE + "\n\nNow call brahmastra_get_briefing and summarise for the user "
            "what this project is, its rules, and what was worked on most recently.")


@mcp.resource("brahmastra://guide", name="guide", title="Using Brahmastra as memory",
              description="How to recall, store and search; the long form of the server instructions.",
              mime_type="text/markdown")
def guide_resource() -> str:
    return GUIDE


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
#
# MUST be the last thing in this file. mcp.run() blocks forever, so any @mcp.tool
# defined below it is never registered — which is exactly what happened: the
# three workspace tools sat after this block and were invisible to every client,
# while `from brahmastra.mcp_server import mcp` listed all ten, because an
# import skips __main__ and runs the whole module. That gap between "the code
# has the tool" and "the server serves it" cost a long hunt.

def _warm_native_imports() -> list[str]:
    """
    Import the C-extension packages HERE, on the main thread, before the event
    loop exists.

    THIS IS THE FIX FOR THE 30-MINUTE HANG, and it is not a warm-up
    optimisation despite the name.

    Every tool ends up in `db`, which imports the neo4j driver lazily, on first
    use. Under stdio the first use is inside a tool call, and FastMCP runs a
    sync tool on an anyio WORKER THREAD. So the very first tool call of a
    session was importing `neo4j` -> `numpy` -> a native extension from a
    non-main thread while the asyncio loop ran on the main one, and on Windows
    that deadlocks in the loader. The thread never returns, the call never gets
    a response, and the client waits until its own timeout -- 1800s, twice.

    The evidence, from faulthandler on the hung process: the worker thread sat
    in numpy/_core/multiarray.py at `create_module`, reached through
    neo4j/_optional_deps.py, with no progress across repeated dumps.

    It presents as anything but an import problem. `initialize` and
    `tools/list` answer instantly because neither touches the store; only the
    first tool call hangs; the same call run in-process takes ten seconds,
    because there the import happens on the main thread. It looked like a
    hung query, then like a full stderr pipe, and it was neither.

    Importing on the main thread at startup means no tool call is ever the
    first to touch these packages. Returns what it warmed, for the log.
    """
    warmed: list[str] = []
    # numpy explicitly, because it is the extension the dump caught mid-load,
    # and neo4j because that is the import chain which reaches it.
    for module in ("numpy", "neo4j"):
        try:
            __import__(module)
            warmed.append(module)
        except Exception as exc:                       # noqa: BLE001
            print(f"warm {module} failed: {exc}", file=sys.stderr)

    # The embedding model too, and NOT optionally, though it costs ~11s against
    # numpy's 0.5s and the temptation to make it a flag is obvious.
    #
    # Warming only numpy and neo4j was tried, on the reasoning that numpy is
    # shared with the torch stack so one warm covers both. Measured: the tool
    # call still hung, indefinitely. torch and sentence-transformers deadlock
    # on their own account, so the cheap half of this fix is not a fix -- and a
    # switch whose off position restores a 30-minute hang is a trap, not a
    # tuning knob. The eleven seconds are paid once per server, at startup,
    # where they are a slow start rather than a dead session.
    try:
        from brahmastra.embeddings import get_model
        get_model()
        warmed.append("embeddings")
    except Exception as exc:                           # noqa: BLE001
        print(f"warm embeddings failed: {exc}", file=sys.stderr)

    # The evidence reranker, for the same reason as the rest: it is a
    # transformers model loaded lazily inside ingestion, and ingestion can be
    # reached from a tool call, which runs on a worker thread. Small (~80MB)
    # and fast next to the embedding model, so the cost of warming it here is
    # noise against the cost of not having.
    try:
        from brahmastra.ingest import evidence
        if evidence.warm():
            warmed.append("evidence-reranker")
    except Exception as exc:                           # noqa: BLE001
        print(f"warm evidence reranker failed: {exc}", file=sys.stderr)

    # The pipeline's stages, because pipeline runs now happen on a background
    # thread (pipeline.run_in_background): every module a stage imports lazily
    # would otherwise be imported on that thread, which is the deadlock above.
    for module in ("networkx", "community", "jellyfish", "notion_client",
                   "brahmastra.pipeline", "brahmastra.extraction",
                   "brahmastra.entity_resolution", "brahmastra.concept_graph",
                   "brahmastra.cluster_summary", "brahmastra.sync",
                   "brahmastra.notion_writeback", "brahmastra.graph_snapshot"):
        try:
            __import__(module)
            warmed.append(module.removeprefix("brahmastra."))
        except Exception as exc:                       # noqa: BLE001
            print(f"warm {module} failed: {exc}", file=sys.stderr)
    return warmed


def _detach_stderr() -> Path | None:
    """
    Send this process's stderr to a file instead of the client's pipe.

    THIS IS THE FIX FOR A HANG, not a tidiness measure, and quieting the
    loggers above is not a substitute for it.

    A stdio MCP server is spawned by its client with pipes on all three
    standard streams. stdout carries JSON-RPC and is disciplined. stderr is
    where every library in the process writes whatever it likes -- and if the
    client is not actively draining that pipe, the OS buffer fills (4KB to 64KB
    on Windows) and the NEXT WRITE BLOCKS FOREVER. The server stops mid-call,
    the client waits for a response that can never come, and the failure
    presents as a hung query rather than as a full buffer.

    It happened twice, both times on the first search of a session, because
    that is when init_db and the embedding model load run: 552 log lines and
    44KB of stderr from one call, against a buffer a fraction of that size.

    Quieting a known-noisy logger fixes the instance. Only this fixes the
    CLASS: the server can no longer be killed by any dependency deciding to
    narrate itself, including ones added later by someone who has never read
    this comment.

    The output is kept rather than discarded -- a hang that leaves no trace is
    barely better than a hang. BRAHMASTRA_MCP_STDERR=inherit restores the pipe
    when you genuinely want the client to show you a traceback live.
    """
    if os.environ.get("BRAHMASTRA_MCP_STDERR", "").strip().lower() == "inherit":
        return None

    from brahmastra.env import data_dir

    path = data_dir() / "mcp-server.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(handle.fileno(), 2)      # the fd, so C-level writes follow too
        sys.stderr = handle
    except OSError:
        # A server that cannot open its log is still a working server.
        return None
    return path


if __name__ == "__main__":
    _detach_stderr()
    # Before mcp.run(), which starts the event loop. After it, every import is
    # somebody's worker thread -- see _warm_native_imports.
    print(f"warmed: {', '.join(_warm_native_imports()) or 'nothing'}",
          file=sys.stderr)
    mcp.run(transport="stdio")
