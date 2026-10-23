"""
Agent hooks: what Brahmastra puts in front of an agent at fixed moments,
whichever client the agent runs in.

Why hooks at all. Measured in evals/agent (2026-10-23): with the server's
instructions alone, or a skill, agents recalled by their own choice in 1 of 24
sessions, and went ahead with a change the team had decided against. With hooks
that put recall in front of the model on every request and check for unstored
decisions at the end of a turn, they declined it and stored the new decision.
Hooks do not ask the agent to decide to look; that is the whole point.

One implementation, four clients. Every client runs a hook as "event JSON in,
JSON out", but each names its events and shapes its output differently:

    moment            Claude Code / Codex   Cursor               Gemini CLI
    request           UserPromptSubmit      beforeSubmitPrompt*  BeforeAgent
    reply finished    Stop                  afterAgentResponse,  AfterAgent**
                                            stop
    session opens     SessionStart          sessionStart         SessionStart

    *  Cursor cannot add context to a request; the hook only records it.
    ** Gemini's AfterAgent can only reject the answer and retry, which would
       throw away a good reply to deliver a reminder. So a Gemini reminder is
       carried to the start of the NEXT turn instead.

`handle(client, payload)` takes the client's own hook input and returns its
own output, so the hook script on the client's side is a pipe with no logic
(plugins/*/hooks/brahmastra-hook.mjs) and every rule here is tested in Python.
Claude Code's MCP-tool hooks (mcp_server.brahmastra_recall) use it too.

Session state (the turn's request, the last reply, whether the briefing was
delivered) is process-local and bounded. Losing it costs a nudge, never a
wrong answer, so it is not worth a table.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any

from brahmastra import db

CLIENTS = ("claude", "codex", "cursor", "gemini")

PROMPT, STOP, START, RESPONSE = "prompt", "stop", "start", "response"

# Each client's event names, mapped to the moment they mark. Anything not
# listed gets an empty answer, so a client adding events breaks nothing.
EVENTS: dict[str, dict[str, str]] = {
    "claude": {"UserPromptSubmit": PROMPT, "Stop": STOP, "SessionStart": START},
    "codex": {"UserPromptSubmit": PROMPT, "Stop": STOP, "SessionStart": START},
    "cursor": {"beforeSubmitPrompt": PROMPT, "stop": STOP, "sessionStart": START,
               "afterAgentResponse": RESPONSE},
    "gemini": {"BeforeAgent": PROMPT, "AfterAgent": RESPONSE, "SessionStart": START},
}

BRIEFING_NOTE_ID = "briefing"
RECALL_NOTES = 4
RECALL_SNIPPET = 360
BRIEFING_CHARS = 2400
TEXT_CHARS = 4000

# Words suggesting a turn settled something a teammate will need: a decision,
# a root cause, a declined request, a changed requirement. A cheap gate, so an
# ordinary "added the function" turn ends without an extra step; the agent,
# which has the whole conversation, makes the actual call. Replaced a
# prompt-type Stop hook (a separate model judging from the last message alone):
# it errored in practice, and Claude Code then showed the agent the hook's own
# instructions, which the agent answered with {"ok": true} as its final reply
# (measured 2026-10-23).
SETTLED = re.compile(
    r"\b(decid\w*|decision|because|reason|root cause|the (issue|problem|cause) was|caused by|"
    r"instead of|declin\w*|can't proceed|cannot proceed|won't|should not|must not|"
    r"drop\w*|deprecat\w*|no longer|policy|requirement|constraint|agreed|sign-off)\b", re.I)

REMINDER = (
    "Brahmastra: if this turn settled something a teammate will need later that the code "
    "and git history cannot tell them (a decision and its reason, a root cause, a changed "
    "requirement, a constraint, a request declined and why), store it now with "
    "brahmastra_add_note, unless it is already stored or was recalled from Brahmastra. "
    "Do not store summaries of code you wrote. Then end your turn with one sentence "
    "telling the user what you did.")

# Gemini: the same check, delivered with the next request (see the docstring).
CARRIED_REMINDER = (
    "Brahmastra: your previous turn may have settled something a teammate will need (a "
    "decision and its reason, a root cause, a changed requirement, a request declined and "
    "why). If it did and it is not stored yet, store it with brahmastra_add_note before "
    "you continue. Do not store summaries of code you wrote.")

RECALL_HEAD = ("Brahmastra memory recalled for this request (the team's stored notes; some may "
               "be unrelated). The user and the project's own files come first; use "
               "brahmastra_search_notes for more.")


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

_SESSIONS_MAX = 512
_sessions: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_lock = threading.Lock()


def _session(session: str) -> dict[str, Any]:
    """The state of one agent session in the current workspace; {} when unnamed."""
    if not session:
        return {}
    key = (db.workspace(), session)
    with _lock:
        state = _sessions.pop(key, None) or {}
        _sessions[key] = state
        while len(_sessions) > _SESSIONS_MAX:
            _sessions.popitem(last=False)
        return state


def reset() -> None:
    with _lock:
        _sessions.clear()


# ---------------------------------------------------------------------------
# What to say
# ---------------------------------------------------------------------------

def _clip(text: Any, limit: int = TEXT_CHARS) -> str:
    return str(text or "")[:limit]


def briefing_text(reason: str) -> str:
    """The workspace briefing as hook context, or "" when there is none."""
    db.init_db()
    note = db.get_note(BRIEFING_NOTE_ID)
    if not note:
        return ""
    body = " ".join(str(note.get("content") or "").split())
    if len(body) > BRIEFING_CHARS:
        body = body[:BRIEFING_CHARS] + "... (brahmastra_get_briefing has the rest)"
    return f"Brahmastra memory: this workspace's briefing, {reason}.\n## {note.get('title')}\n{body}"


def recall_text(prompt: str) -> str:
    """The notes matching a request, or "" when nothing matches."""
    query = " ".join((prompt or "").split())[:500]
    if len(query) < 8:
        return ""
    db.init_db()
    # The briefing has its own delivery (session start or first request).
    notes = [n for n in db.search_notes(query, limit=RECALL_NOTES + 1)
             if n.get("id") != BRIEFING_NOTE_ID][:RECALL_NOTES]
    if not notes:
        return ""
    lines = []
    for n in notes:
        text = " ".join((n.get("content") or "").split())
        text = text if len(text) <= RECALL_SNIPPET else text[:RECALL_SNIPPET] + "..."
        lines.append(f"- {n.get('title')} (note {n.get('id')}): {text}")
    return RECALL_HEAD + "\n" + "\n".join(lines)


def settled(*texts: str) -> bool:
    return bool(SETTLED.search(" ".join(t or "" for t in texts)))


def on_start(session: str, source: str = "startup") -> str:
    state = _session(session)
    state["briefed"] = True
    reason = "re-read after compaction" if source == "compact" else "at the start of this session"
    return briefing_text(reason)


def on_prompt(session: str, prompt: str) -> str:
    """Recall for a request; the briefing too on a session's first one.

    The first-request briefing exists because a session-start hook cannot
    always deliver it: Claude Code skips MCP-tool hooks at launch (the server
    is not connected yet), and Codex 0.154 rejected SessionStart context.
    """
    state = _session(session)
    parts = []
    if session and not state.get("briefed"):
        state["briefed"] = True
        parts.append(briefing_text("at the start of this session"))
    if state.pop("carry_reminder", False):
        parts.append(CARRIED_REMINDER)
    if session:
        state["request"] = _clip(prompt)
    parts.append(recall_text(prompt))
    return "\n\n".join(p for p in parts if p)


def on_stop(session: str, last_message: str | None = None, active: bool = False) -> str:
    """The store reminder, once per turn, when the turn reads as settling something."""
    state = _session(session)
    if active:
        return ""
    reply = last_message if last_message is not None else state.get("response", "")
    return REMINDER if settled(reply, state.get("request", "")) else ""


def on_response(session: str, reply: str, carry: bool) -> None:
    if not session:
        return
    state = _session(session)
    state["response"] = _clip(reply)
    if carry and settled(reply, state.get("request", "")):
        state["carry_reminder"] = True


# ---------------------------------------------------------------------------
# Each client's input and output
# ---------------------------------------------------------------------------

def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def handle(client: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The client's hook output for the client's hook input; {} for "nothing to add"."""
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    moment = EVENTS.get(client, {}).get(event)
    if moment is None:
        return {}
    session = str(payload.get("session_id") or payload.get("conversation_id") or "")

    if moment == START:
        text = on_start(session, str(payload.get("source") or "startup"))
    elif moment == PROMPT:
        text = on_prompt(session, str(payload.get("prompt") or ""))
    elif moment == RESPONSE:
        on_response(session, str(payload.get("text") or payload.get("prompt_response") or ""),
                    carry=client == "gemini")
        text = ""
    else:
        if client == "cursor":
            # Cursor's stop says nothing about the reply (afterAgentResponse
            # did), and a follow-up message it sends counts as a loop.
            text = (on_stop(session, None, active=int(payload.get("loop_count") or 0) > 0)
                    if payload.get("status", "completed") == "completed" else "")
        else:
            text = on_stop(session, str(payload.get("last_assistant_message") or ""),
                           active=_truthy(payload.get("stop_hook_active")))

    return render(client, event, moment, text)


def render(client: str, event: str, moment: str, text: str) -> dict[str, Any]:
    if not text:
        return {}
    if client == "cursor":
        if moment == START:
            return {"additional_context": text}
        if moment == STOP:
            return {"followup_message": text}
        return {}
    if client == "codex" and moment == STOP:
        # Codex continues the turn with `reason` as the next prompt.
        return {"decision": "block", "reason": text}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
