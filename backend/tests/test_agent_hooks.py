"""
Agent hooks (brahmastra/agent_hooks.py): the same memory at the same moments in
Claude Code, Codex, Cursor and Gemini CLI, each in the shape that client reads.

The shapes are each client's documented hook contract (checked 2026-10-23):
  Claude Code, Codex, Gemini  hookSpecificOutput.additionalContext
  Codex Stop                  {"decision": "block", "reason": ...} continues the turn
  Cursor sessionStart         {"additional_context": ...}
  Cursor stop                 {"followup_message": ...} is sent as the next message
A wrong shape is not an error anywhere: the client drops it silently, which is
how the first recall hook reached nobody. So the shapes are pinned here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from brahmastra import agent_hooks

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"
SCRIPT_HOMES = ("brahmastra-codex", "brahmastra-cursor", "brahmastra-gemini")


@pytest.fixture
def store(monkeypatch, tmp_path):
    from brahmastra import db
    from brahmastra.stores import reset_store

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "hooks.db"))
    reset_store()
    agent_hooks.reset()
    db.init_db()
    db.upsert_note("keep-due", "Decision: keep the field named due",
                   "The mobile app parses the key due; renaming Task.due breaks it.", mark_pending=False)
    yield db
    agent_hooks.reset()
    reset_store()


def _briefing(store):
    store.upsert_note("briefing", "Larkspur briefing", "Releases go out on Tuesdays.", mark_pending=False)


RENAME = "rename the due field to due_at"
SETTLED_REPLY = "I can't proceed: the team decided to keep the field named due because the app parses it."


# ---------------------------------------------------------------------------
# Recall on a request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client, event", [
    ("claude", "UserPromptSubmit"), ("codex", "UserPromptSubmit"), ("gemini", "BeforeAgent")])
def test_recall_reaches_each_client_in_its_own_shape(store, client, event):
    out = agent_hooks.handle(client, {"hook_event_name": event, "prompt": RENAME, "session_id": "s"})
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == event
    assert "keep the field named due" in hook["additionalContext"]
    assert "project's own files come first" in hook["additionalContext"]


def test_cursor_cannot_take_context_on_a_request_so_it_is_only_recorded(store):
    out = agent_hooks.handle("cursor", {"hook_event_name": "beforeSubmitPrompt", "prompt": RENAME,
                                        "conversation_id": "c"})
    assert out == {}, "beforeSubmitPrompt accepts only continue/user_message"


def test_the_briefing_comes_with_a_sessions_first_request_once(store):
    """Claude Code skips MCP hooks at launch, so the first request carries it."""
    _briefing(store)
    first = agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "prompt": RENAME,
                                          "session_id": "s1"})
    text = first["hookSpecificOutput"]["additionalContext"]
    assert "Tuesdays" in text and "keep the field named due" in text
    second = agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "prompt": RENAME,
                                           "session_id": "s1"})
    assert "Tuesdays" not in second["hookSpecificOutput"]["additionalContext"]
    assert text.count("Tuesdays") == 1, "the briefing is not recalled a second time as a note"


def test_a_briefing_delivered_at_session_start_is_not_repeated(store):
    _briefing(store)
    start = agent_hooks.handle("codex", {"hook_event_name": "SessionStart", "source": "startup",
                                         "session_id": "s2"})
    assert "Tuesdays" in start["hookSpecificOutput"]["additionalContext"]
    out = agent_hooks.handle("codex", {"hook_event_name": "UserPromptSubmit", "prompt": RENAME,
                                       "session_id": "s2"})
    assert "Tuesdays" not in out["hookSpecificOutput"]["additionalContext"]


def test_cursor_gets_the_briefing_at_session_start(store):
    _briefing(store)
    out = agent_hooks.handle("cursor", {"hook_event_name": "sessionStart", "session_id": "c1"})
    assert "Tuesdays" in out["additional_context"]


def test_nothing_to_add_is_an_empty_answer(store):
    assert agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "prompt": "ok"}) == {}
    assert agent_hooks.handle("claude", {"hook_event_name": "SessionStart"}) == {}, "no briefing"
    assert agent_hooks.handle("claude", {"hook_event_name": "PreToolUse"}) == {}, "unknown event"
    assert agent_hooks.handle("nobody", {"hook_event_name": "Stop"}) == {}


# ---------------------------------------------------------------------------
# The store reminder at the end of a turn
# ---------------------------------------------------------------------------

def test_codex_stop_continues_the_turn_with_the_reminder(store):
    out = agent_hooks.handle("codex", {"hook_event_name": "Stop", "session_id": "s",
                                       "last_assistant_message": SETTLED_REPLY,
                                       "stop_hook_active": False})
    assert out["decision"] == "block" and "brahmastra_add_note" in out["reason"]
    assert agent_hooks.handle("codex", {"hook_event_name": "Stop", "session_id": "s",
                                        "last_assistant_message": SETTLED_REPLY,
                                        "stop_hook_active": True}) == {}, "once per turn"


def test_routine_work_ends_without_a_reminder(store):
    assert agent_hooks.handle("codex", {"hook_event_name": "Stop", "session_id": "s",
                                        "last_assistant_message": "Added overdue() and exported it."}) == {}


def test_the_reason_in_the_request_counts(store):
    """The reply often says only "done"; the reason was in the request."""
    agent_hooks.handle("codex", {"hook_event_name": "UserPromptSubmit", "session_id": "s3",
                                 "prompt": "Drop Python 3.9: it is end-of-life."})
    out = agent_hooks.handle("codex", {"hook_event_name": "Stop", "session_id": "s3",
                                       "last_assistant_message": "Done. Updated requires-python."})
    assert out["decision"] == "block"


def test_cursor_stop_uses_the_recorded_reply_and_follows_up_once(store):
    agent_hooks.handle("cursor", {"hook_event_name": "afterAgentResponse", "conversation_id": "c2",
                                  "text": SETTLED_REPLY})
    out = agent_hooks.handle("cursor", {"hook_event_name": "stop", "conversation_id": "c2",
                                        "status": "completed", "loop_count": 0})
    assert "brahmastra_add_note" in out["followup_message"]
    assert agent_hooks.handle("cursor", {"hook_event_name": "stop", "conversation_id": "c2",
                                         "status": "completed", "loop_count": 1}) == {}
    assert agent_hooks.handle("cursor", {"hook_event_name": "stop", "conversation_id": "c2",
                                         "status": "aborted", "loop_count": 0}) == {}


def test_gemini_carries_the_reminder_to_the_next_request(store):
    """AfterAgent can only reject the answer, so the reminder waits a turn."""
    after = agent_hooks.handle("gemini", {"hook_event_name": "AfterAgent", "session_id": "g",
                                          "prompt": RENAME, "prompt_response": SETTLED_REPLY})
    assert after == {}, "never reject a good answer to deliver a reminder"
    out = agent_hooks.handle("gemini", {"hook_event_name": "BeforeAgent", "session_id": "g",
                                        "prompt": "now add overdue()"})
    assert "previous turn may have settled" in out["hookSpecificOutput"]["additionalContext"]
    again = agent_hooks.handle("gemini", {"hook_event_name": "BeforeAgent", "session_id": "g",
                                          "prompt": "and export it from the package"})
    assert "previous turn" not in json.dumps(again)


# ---------------------------------------------------------------------------
# Noise (2026-10-28): recall on "yes go ahead", a reminder after nearly every turn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", ["yes", "yes go ahead", "merge it", "ok continue", "lets do this now"])
def test_a_short_reply_is_not_searched(store, reply):
    """It continues the previous turn, whose notes were already recalled."""
    agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "q",
                                  "prompt": RENAME})
    assert agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "q",
                                         "prompt": reply}) == {}


def test_a_background_task_notification_is_not_a_request(store):
    """Claude Code submits finished background tasks as prompts; the user asked nothing."""
    _briefing(store)
    agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "n",
                                  "prompt": "Drop Python 3.9: it is end-of-life."})
    note = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n<summary>"
            "Background command \"rename the due field to due_at\" completed</summary>\n</task-notification>")
    assert agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "n",
                                         "prompt": note}) == {}
    out = agent_hooks.handle("claude", {"hook_event_name": "Stop", "session_id": "n",
                                        "last_assistant_message": "Done."})
    assert "brahmastra_add_note" in out["hookSpecificOutput"]["additionalContext"], \
        "the user's request still counts for the Stop check"


def test_a_short_first_request_still_brings_the_briefing(store):
    _briefing(store)
    out = agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "q1",
                                        "prompt": "continue"})
    assert "Tuesdays" in out["hookSpecificOutput"]["additionalContext"]


def test_no_reminder_once_the_agent_stored_a_note_this_turn(store):
    stop = {"hook_event_name": "Stop", "session_id": "r", "last_assistant_message": SETTLED_REPLY}
    agent_hooks.note_stored()                      # a note from an EARLIER turn
    agent_hooks.handle("codex", {"hook_event_name": "UserPromptSubmit", "session_id": "r",
                                 "prompt": RENAME})
    assert agent_hooks.handle("codex", stop)["decision"] == "block", "an older store does not count"
    agent_hooks.note_stored()
    assert agent_hooks.handle("codex", stop) == {}, "stored this turn: nothing to remind"


def test_storing_through_the_mcp_tool_counts(store):
    from brahmastra import mcp_server

    mcp_server.brahmastra_recall(prompt=RENAME, event="UserPromptSubmit", session="m")
    mcp_server.brahmastra_add_note("Decision: keep due", "Tomas Weil decided to keep the field due.")
    assert mcp_server.brahmastra_recall(prompt=SETTLED_REPLY, event="Stop", active="false",
                                        session="m") == ""


@pytest.mark.parametrize("text, expected", [
    ("I used a dict because lookups are O(1).", False),
    ("Renamed it instead of adding a wrapper; the tests should not need changes.", False),
    ("I won't touch the CI config.", False),
    ("Drop Python 3.9: it is end-of-life.", True),
    ("I can't proceed: the team decided to keep the field named due.", True),
    ("The root cause was a naive datetime compared with UTC.", True),
    ("Renames need Tomas Weil's sign-off.", True),
])
def test_only_words_that_mark_a_settled_matter_count(text, expected):
    assert agent_hooks.settled(text) is expected


def test_a_hook_initialises_the_store_once_not_per_prompt(store, monkeypatch):
    """init_db costs ~3 s against Postgres plus a remote Neo4j; hooks run every prompt."""
    from brahmastra import db

    calls = []
    real = db.init_db
    monkeypatch.setattr(db, "init_db", lambda: (calls.append(1), real())[1])
    agent_hooks._ready.clear()
    for i in range(3):
        agent_hooks.handle("claude", {"hook_event_name": "UserPromptSubmit", "session_id": f"i{i}",
                                      "prompt": RENAME})
    assert len(calls) == 1, calls


def test_session_state_stays_bounded(store, monkeypatch):
    monkeypatch.setattr(agent_hooks, "_SESSIONS_MAX", 3)
    for i in range(10):
        agent_hooks.handle("codex", {"hook_event_name": "UserPromptSubmit", "session_id": f"s{i}",
                                     "prompt": "x"})
    assert len(agent_hooks._sessions) == 3


# ---------------------------------------------------------------------------
# POST /hooks
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "route.db"))
    monkeypatch.setenv("RATE_LIMITS", "off")
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    db_mod.upsert_note("keep-due", "Decision: keep the field named due",
                       "The mobile app parses the key due.", mark_pending=False)
    from brahmastra import accounts, auth
    import main
    importlib.reload(main)
    auth.reset_for_tests()
    agent_hooks.reset()
    return main.app, accounts.store()


def test_a_viewer_token_can_call_the_hook_route(app):
    from fastapi.testclient import TestClient

    application, accounts = app
    vic = accounts.create_user("vic@example.com", "correct horse battery staple", role="viewer",
                               workspaces=["default"])
    token = accounts.create_token(vic["id"], "laptop")["token"]
    with TestClient(application) as client:
        ok = client.post("/hooks?client=codex", headers={"Authorization": f"Bearer {token}"},
                         json={"hook_event_name": "UserPromptSubmit", "prompt": RENAME, "session_id": "h"})
        assert ok.status_code == 200
        assert "keep the field named due" in ok.json()["hookSpecificOutput"]["additionalContext"]
        assert client.post("/hooks?client=codex", json={}).status_code == 401
        bad = client.post("/hooks?client=vim", headers={"Authorization": f"Bearer {token}"}, json={})
        assert bad.status_code == 400
        junk = client.post("/hooks?client=codex", headers={"Authorization": f"Bearer {token}"},
                           content=b"[1, 2]")
        assert junk.status_code == 400


# ---------------------------------------------------------------------------
# The hook script and the packages
# ---------------------------------------------------------------------------

def test_every_package_ships_the_same_script():
    copies = {d: (PLUGINS / d / "hooks" / "brahmastra-hook.mjs").read_bytes() for d in SCRIPT_HOMES}
    assert len(set(copies.values())) == 1, "edit plugins/brahmastra-codex/hooks and copy it"


@pytest.mark.parametrize("package, client, path", [
    ("brahmastra-codex", "codex", "hooks/hooks.json"),
    ("brahmastra-cursor", "cursor", "hooks/hooks.json"),
    ("brahmastra-gemini", "gemini", "hooks/hooks.json"),
])
def test_each_package_hooks_only_events_the_server_understands(package, client, path):
    hooks = json.loads((PLUGINS / package / path).read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) <= set(agent_hooks.EVENTS[client]), set(hooks) - set(agent_hooks.EVENTS[client])
    commands = []
    for entries in hooks.values():
        for entry in entries:
            for hook in entry.get("hooks", [entry]):
                commands += [hook["command"], hook.get("commandWindows", hook["command"])]
    assert commands
    for command in commands:
        assert "brahmastra-hook.mjs" in command and command.endswith(f" {client}"), command


def test_the_packages_agree_on_a_version():
    versions = {
        json.loads((PLUGINS / "brahmastra/.claude-plugin/plugin.json").read_text())["version"],
        json.loads((PLUGINS / "brahmastra-codex/.codex-plugin/plugin.json").read_text())["version"],
        json.loads((PLUGINS / "brahmastra-cursor/.cursor-plugin/plugin.json").read_text())["version"],
        json.loads((PLUGINS / "brahmastra-gemini/gemini-extension.json").read_text())["version"],
    }
    assert len(versions) == 1, versions


def test_the_codex_marketplace_lists_the_codex_package():
    market = json.loads((REPO / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    paths = [p["source"]["path"] for p in market["plugins"]]
    assert paths == ["./plugins/brahmastra-codex"]
    assert (REPO / paths[0].lstrip("./") / ".codex-plugin/plugin.json").exists()


NODE = shutil.which("node")


class _Stub(BaseHTTPRequestHandler):
    seen: list = []
    reply = b'{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "hi"}}'

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Stub.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                           "workspace": self.headers.get("X-Brahmastra-Workspace"), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_Stub.reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    _Stub.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _run(env, stdin, home):
    script = PLUGINS / "brahmastra-codex" / "hooks" / "brahmastra-hook.mjs"
    base = {k: v for k, v in os.environ.items() if not k.startswith("BRAHMASTRA_")}
    base.update(HOME=str(home), USERPROFILE=str(home), **env)
    return subprocess.run([NODE, str(script), "codex"], input=stdin, capture_output=True, text=True,
                          env=base, timeout=30)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_script_is_a_pipe_to_the_server(stub, tmp_path):
    payload = '{"hook_event_name": "UserPromptSubmit", "prompt": "hello"}'
    done = _run({"BRAHMASTRA_URL": stub + "/", "BRAHMASTRA_TOKEN": "fake-token-123",
                 "BRAHMASTRA_WORKSPACE": "larkspur"}, payload, tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] == "hi"
    seen = _Stub.seen[-1]
    assert seen["path"] == "/hooks?client=codex"
    assert seen["auth"] == "Bearer fake-token-123" and seen["workspace"] == "larkspur"
    assert json.loads(seen["body"]) == json.loads(payload)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_script_reads_a_config_file_when_no_variable_is_set(stub, tmp_path):
    (tmp_path / ".brahmastra").mkdir()
    (tmp_path / ".brahmastra" / "agent.json").write_text(
        json.dumps({"url": stub, "token": "file-token-456"}), encoding="utf-8")
    done = _run({}, "{}", tmp_path)
    assert done.returncode == 0, done.stderr
    assert _Stub.seen[-1]["auth"] == "Bearer file-token-456"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_a_token_never_crosses_the_network_unencrypted(tmp_path):
    """Plain http only to this machine; the file's address is not trusted further."""
    done = _run({"BRAHMASTRA_URL": "http://kb.example.invalid", "BRAHMASTRA_TOKEN": "fake-token-789"},
                '{"hook_event_name": "Stop"}', tmp_path)
    assert done.returncode == 0 and done.stdout == ""
    assert "refusing to send a token over plain http" in done.stderr


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_script_never_fails_the_turn(tmp_path):
    done = _run({"BRAHMASTRA_URL": "http://127.0.0.1:9", "BRAHMASTRA_HOOK_TIMEOUT_MS": "3000"},
                '{"hook_event_name": "Stop"}', tmp_path)
    assert done.returncode == 0
    assert done.stdout == "", "nothing printed, so the client adds nothing"
    assert "unreachable" in done.stderr
