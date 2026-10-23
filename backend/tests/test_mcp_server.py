"""
Tests for the MCP server's tool surface.

These exist because of a bug that no import-based test could have caught: the
`if __name__ == "__main__": mcp.run()` block sat in the MIDDLE of the module,
above three @mcp.tool definitions. mcp.run() blocks forever, so running the
server registered only the tools declared above it, while
`from brahmastra.mcp_server import mcp` registered all ten — an import never
executes __main__. The code had the tools; the running server did not serve
them, and the two views disagreed for days.
"""
from __future__ import annotations

import asyncio
import runpy
from unittest.mock import patch

import pytest

# The MCP SDK is installed only in backend/.venv, which is what actually runs
# the server; the global interpreter runs the suite. Skip rather than fail
# there, and run these explicitly with:
#     .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -q
pytest.importorskip("mcp", reason="mcp SDK is installed in backend/.venv only")

EXPECTED = {
    "brahmastra_run_pipeline",
    "brahmastra_get_graph_stats",
    "brahmastra_search_entities",
    "brahmastra_search_notes",
    "brahmastra_search_sessions",
    "brahmastra_search_code",
    "brahmastra_get_entity_details",
    "brahmastra_get_contradictions",
    "brahmastra_add_note",
    "brahmastra_list_workspaces",
    "brahmastra_create_workspace",
    "brahmastra_search_all_workspaces",
    "brahmastra_get_briefing",
    "brahmastra_recall",
}


def _tools_registered_as_main() -> set[str]:
    """Register tools the way the server does — as __main__, without blocking."""
    with patch("mcp.server.fastmcp.FastMCP.run", lambda self, **kw: None):
        module = runpy.run_module("brahmastra.mcp_server", run_name="__main__")
    return {t.name for t in asyncio.run(module["mcp"].list_tools())}


def test_every_tool_is_registered_when_run_as_the_server_runs_it():
    registered = _tools_registered_as_main()
    missing = EXPECTED - registered
    assert not missing, (
        f"declared but never registered: {sorted(missing)}. A tool defined below "
        "mcp.run() is invisible to clients — keep the entrypoint last."
    )
    assert registered == EXPECTED


def test_the_entrypoint_is_the_last_thing_in_the_file():
    """
    The structural guarantee behind the test above. Asserting position as well
    as behaviour means a tool added below the entrypoint fails loudly here,
    naming the cause, rather than only showing up as a missing tool.
    """
    import inspect
    from brahmastra import mcp_server

    source = inspect.getsource(mcp_server)
    run_at = source.index("mcp.run(transport=")
    assert "@mcp.tool" not in source[run_at:], (
        "a tool is defined after mcp.run(); it will never be registered"
    )


# ---------------------------------------------------------------------------
# What an agent is told, and whether it all arrives
# ---------------------------------------------------------------------------

import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

# Claude Code cuts server instructions, and the description of a tool loaded
# upfront, at 4,096 characters, silently. The instructions ran to 4,127 for
# weeks, so every session read a clipped ending.
CLIENT_LIMIT = 4_096


def _server():
    from brahmastra import mcp_server
    return mcp_server


def test_the_instructions_fit_with_room_to_spare():
    m = _server()
    assert len(m.INSTRUCTIONS) <= m.INSTRUCTIONS_BUDGET < CLIENT_LIMIT, (
        f"instructions are {len(m.INSTRUCTIONS)} characters; clients cut at "
        f"{CLIENT_LIMIT}. Move detail into GUIDE instead.")


def test_the_loop_comes_first_in_the_instructions():
    """What survives a cut, or a skim, must be the loop itself."""
    head = _server().INSTRUCTIONS[:1_200]
    for tool in ("brahmastra_get_briefing", "brahmastra_search_notes", "brahmastra_add_note"):
        assert tool in head


def test_every_tool_description_fits_the_client_limit():
    for tool in asyncio.run(_server().mcp.list_tools()):
        assert len(tool.description or "") < CLIENT_LIMIT, tool.name


def test_the_instructions_and_guide_name_only_real_tools():
    """A renamed tool left behind in the text sends agents after nothing."""
    m = _server()
    registered = {t.name for t in asyncio.run(m.mcp.list_tools())}
    short = {name.removeprefix("brahmastra_") for name in registered}
    for text in (m.INSTRUCTIONS, m.GUIDE):
        for name in re.findall(r"brahmastra_\w+", text):
            assert name in registered, name
    table = re.findall(r"^\| (\w+) \|", m.GUIDE, re.M)
    assert set(table) - {"tool"} == short, "the GUIDE's tool table is out of step"


def test_every_tool_says_whether_it_changes_anything():
    writes = {"brahmastra_add_note", "brahmastra_create_workspace", "brahmastra_run_pipeline"}
    for tool in asyncio.run(_server().mcp.list_tools()):
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is (tool.name not in writes), tool.name


def test_prompts_and_the_guide_resource_are_served():
    m = _server()
    prompts = {p.name for p in asyncio.run(m.mcp.list_prompts())}
    assert prompts == {"recall", "remember", "onboard"}
    resources = {str(r.uri) for r in asyncio.run(m.mcp.list_resources())}
    assert "brahmastra://guide" in resources
    recall = asyncio.run(m.mcp.get_prompt("recall", {"topic": "the notion leak"}))
    assert "the notion leak" in recall.messages[0].content.text


# ---------------------------------------------------------------------------
# The briefing: a project's context lives in its workspace
# ---------------------------------------------------------------------------

@pytest.fixture
def store(monkeypatch, tmp_path):
    from brahmastra import db
    from brahmastra.stores import reset_store

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "mcp.db"))
    reset_store()
    db.init_db()
    yield db
    reset_store()


def test_a_workspace_without_a_briefing_says_how_to_write_one(store):
    out = json.loads(_server().brahmastra_get_briefing())
    assert out["briefing"] is None
    assert "note_id='briefing'" in out["write_a_briefing"]
    assert out["guide"].startswith("# Using Brahmastra")


def test_the_briefing_and_recent_notes_come_back(store):
    store.upsert_note("briefing", "Apollo briefing", "Apollo is the billing service.",
                      mark_pending=False)
    from datetime import datetime, timedelta, timezone

    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    store.upsert_note("n1", "Older", "x", last_edited=ago(5), mark_pending=False)
    store.upsert_note("n2", "Newer", "y", last_edited=ago(1), mark_pending=False)
    store.upsert_note("n3", "Months old", "z", last_edited=ago(120), mark_pending=False)
    store.upsert_note("n4", "Never stamped", "w", mark_pending=False)
    out = json.loads(_server().brahmastra_get_briefing())
    assert out["briefing"]["content"] == "Apollo is the billing service."
    assert [n["id"] for n in out["recent_notes"]] == ["n2", "n1"],         "recent means the last 30 days, not the newest of whatever is dated"
    assert out["memory"]["notes"] == 5
    assert "write_a_briefing" not in out


# ---------------------------------------------------------------------------
# A rebuild never holds the server
# ---------------------------------------------------------------------------
#
# A synchronous tool runs on the server's event loop, so while it runs the
# server reads nothing else. brahmastra_run_pipeline used to run the pipeline
# right there: a minute when healthy, 30 when something under it hung, and the
# client could only give up.

def _wait_for_background(timeout=10.0):
    from brahmastra import pipeline
    deadline = time.time() + timeout
    while pipeline.background_runs() and time.time() < deadline:
        time.sleep(0.05)
    assert not pipeline.background_runs()


def test_run_pipeline_returns_before_the_run_finishes(store, monkeypatch):
    from brahmastra import pipeline

    release = threading.Event()
    calls = []

    def slow_run(full=False, graph_only=False):
        calls.append(full)
        release.wait(10)

    monkeypatch.setattr(pipeline, "run_pipeline", slow_run)
    monkeypatch.setattr(pipeline, "dirty_since", lambda: None)
    started = time.time()
    first = json.loads(_server().brahmastra_run_pipeline())
    assert time.time() - started < 1.0
    assert first["started"] is True
    second = json.loads(_server().brahmastra_run_pipeline())
    assert second["started"] is False, "two runs for one graph at once"
    release.set()
    _wait_for_background()
    assert calls == [False]


def test_a_background_run_builds_the_callers_workspace(store, monkeypatch):
    """A bare thread starts with no workspace bound and would build `default`."""
    from brahmastra import pipeline
    from brahmastra.workspace import (current_workspace, reset_request_workspace,
                                      set_request_workspace)

    seen = []
    monkeypatch.setattr(pipeline, "run_pipeline",
                        lambda full=False, graph_only=False: seen.append(current_workspace()))
    monkeypatch.setattr(pipeline, "dirty_since", lambda: None)
    token = set_request_workspace("office")
    try:
        pipeline.run_in_background(graph_only=True)
    finally:
        reset_request_workspace(token)
    _wait_for_background()
    assert seen == ["office"]


def test_adding_a_note_refreshes_the_graph_by_itself(store, monkeypatch):
    from brahmastra import extraction, pipeline

    runs = []
    monkeypatch.setenv("GRAPH_AUTO_REFRESH", "1")
    monkeypatch.setenv("GRAPH_REFRESH_DELAY_S", "0")
    monkeypatch.setattr(extraction, "extract_note", lambda note: {"triples_added": 2})
    monkeypatch.setattr(pipeline, "run_pipeline",
                        lambda full=False, graph_only=False: runs.append(graph_only))
    monkeypatch.setattr(pipeline, "dirty_since", lambda: None)
    out = json.loads(_server().brahmastra_add_note(
        "Apollo owner", "Mei Lin owns the Apollo service."))
    assert out["graph"] == "refreshing in the background"
    _wait_for_background()
    assert runs == [True], "a graph-only rebuild: no sync, no model calls"
    assert store.get_note(out["note_id"])["last_edited"], "stored notes carry a time now"


def test_the_refresh_can_be_turned_off(store, monkeypatch):
    from brahmastra import extraction, pipeline

    monkeypatch.setattr(extraction, "extract_note", lambda note: {"triples_added": 0})
    out = json.loads(_server().brahmastra_add_note("t", "Mei Lin owns Apollo."))
    assert "GRAPH_AUTO_REFRESH=0" in out["graph"]
    assert not pipeline.background_runs()


def test_the_stage_modules_are_imported_before_the_event_loop_starts():
    """Pipeline runs happen on a thread now; a module first imported there can
    deadlock the Windows loader (see _warm_native_imports)."""
    import inspect
    source = inspect.getsource(_server()._warm_native_imports)
    for module in ("brahmastra.pipeline", "brahmastra.entity_resolution",
                   "brahmastra.concept_graph", "brahmastra.cluster_summary",
                   "notion_client", "community", "jellyfish"):
        assert f'"{module}"' in source, module


# ---------------------------------------------------------------------------
# Recall for hooks: only JSON additionalContext reaches the model
# ---------------------------------------------------------------------------
#
# Measured 2026-10-23: a plugin hook calling brahmastra_search_notes ran and
# fetched the right notes, and the agent saw only "UserPromptSubmit completed".
# Claude Code passes an MCP hook's result to the model only as JSON with
# hookSpecificOutput.additionalContext.

def test_recall_returns_what_a_hook_can_deliver(store):
    store.upsert_note("keep-due", "Decision: keep the field named due",
                      "The mobile app parses the key due; renaming Task.due breaks it.", mark_pending=False)
    out = json.loads(_server().brahmastra_recall(prompt="rename the due field to due_at"))
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == "UserPromptSubmit"
    assert "keep the field named due" in hook["additionalContext"]
    assert "project's own files come first" in hook["additionalContext"]


def test_recall_after_compaction_brings_back_the_briefing(store):
    store.upsert_note("briefing", "Acme briefing", "Releases go out on Tuesdays.", mark_pending=False)
    out = json.loads(_server().brahmastra_recall(event="SessionStart"))
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Tuesdays" in out["hookSpecificOutput"]["additionalContext"]


def test_recall_adds_nothing_when_there_is_nothing_to_add(store):
    assert _server().brahmastra_recall(prompt="ok") == ""
    assert _server().brahmastra_recall(prompt="something nobody ever wrote about zqxj") == ""
    assert _server().brahmastra_recall(event="SessionStart") == "", "no briefing, no injection"


def test_stop_reminds_once_and_only_when_something_was_settled(store):
    settled = "I can't proceed: the team decided to keep the field named due because the mobile app parses it."
    out = json.loads(_server().brahmastra_recall(prompt=settled, event="Stop", active="false"))
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "brahmastra_add_note" in out["hookSpecificOutput"]["additionalContext"]
    assert _server().brahmastra_recall(prompt=settled, event="Stop", active="true") == "",         "a turn already continued by this hook must be allowed to end"
    assert _server().brahmastra_recall(prompt="Added overdue() to larkspur/core.py and exported it.",
                                       event="Stop", active="false") == "", "routine work costs no extra step"


def test_stop_also_reads_the_request_that_started_the_turn(store):
    """The reason for a decision is often only in the request; the reply just says "done"."""
    _server().brahmastra_recall(prompt="Drop Python 3.9: it is end-of-life.", event="UserPromptSubmit",
                                session="s-1")
    out = _server().brahmastra_recall(prompt="Done. Updated requires-python to >=3.10.", event="Stop",
                                      active="false", session="s-1")
    assert "brahmastra_add_note" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert _server().brahmastra_recall(prompt="Done. Updated requires-python to >=3.10.", event="Stop",
                                       active="false", session="other") == ""
