"""
Plans: which features a person may use, and how much of them.

A plan sits BESIDE a role (auth.required_role), never instead of it. The role
answers "may this person do this, here?" -- a security question, per
workspace. The plan answers "which features, and how much?" -- a cost
question, per person. A request needs both to say yes, so changing a plan can
never let anyone into a workspace or past a role: a viewer on the largest plan
still cannot write.

Plans are ROWS (accounts.AccountStore), not code, so an admin can change a
limit without a release. This module holds what code must know: the feature
names, which route or MCP tool needs which feature, the limit keys, and the
four plans an install starts with.

ROLLOUT AND NEW PEOPLE. Everyone starts on the plan matching their role
(ROLE_PLAN): existing users when plans first appear, so nobody loses access by
upgrading, and new users unless the admin creating them picks another plan.
Starting everyone on the smallest plan was considered and rejected: an admin
who creates someone as a MEMBER means them to write, and a Reader plan would
silently make that role mean nothing.

Limits are stored from the start but ENFORCED in later phases: counting
(questions, notes, words, meeting hours) and then tokens. None means no limit.
"""

from __future__ import annotations

from typing import Any

# Feature id -> how a person sees it named. Order is the order screens show.
FEATURES: dict[str, str] = {
    "ask": "Ask",
    "browse": "Search and browse",
    "graph": "Graph explorer",
    "add_notes": "Add notes",
    "mcp": "AI assistant access (MCP)",
    "documents": "Document upload",
    "meetings": "Meetings",
    "cross_workspace": "Cross-workspace search",
}

LIMITS: dict[str, str] = {
    "questions_month": "Questions per month",
    "questions_day": "Questions per day",
    "notes_month": "Notes per month",
    "notes_day": "Notes per day",
    "words_month": "Words stored per month",
    "meeting_hours_month": "Meeting hours per month",
    "tokens_month": "Token budget per month",
    "tokens_day": "Token budget per day",
}

# Searching costs no tokens, so every plan can search everywhere it is a member,
# one workspace at a time or across them. MCP is in every plan too: connecting an assistant costs nothing, and its reading
# tools are free. What it can WRITE is checked per tool (TOOL_FEATURES), so a
# Reader's assistant searches but cannot add a note. Leaving it out of Reader
# would also have taken back the remote MCP every role gained before plans.
_READ = ["ask", "browse", "graph", "mcp", "cross_workspace"]
_WRITE = _READ + ["add_notes", "documents"]

DEFAULT_PLANS: list[dict[str, Any]] = [
    {"id": "reader", "name": "Reader", "description": "Asks questions and explores; adds nothing.",
     "features": _READ,
     "limits": {"questions_month": 100, "questions_day": 10, "notes_month": 0, "notes_day": 0,
                "words_month": 0, "meeting_hours_month": 0,
                "tokens_month": 1_500_000, "tokens_day": 150_000}},
    {"id": "contributor", "name": "Contributor",
     "description": "Adds knowledge from notes, documents and AI assistants.",
     "features": _WRITE,
     "limits": {"questions_month": 300, "questions_day": 25, "notes_month": 300, "notes_day": 50,
                "words_month": 200_000, "meeting_hours_month": 0,
                "tokens_month": 5_000_000, "tokens_day": 500_000}},
    {"id": "power", "name": "Power", "description": "Everything, including meetings.",
     "features": _WRITE + ["meetings"],
     "limits": {"questions_month": 800, "questions_day": 60, "notes_month": 1_000, "notes_day": 150,
                "words_month": 600_000, "meeting_hours_month": 5,
                "tokens_month": 15_000_000, "tokens_day": 1_500_000}},
    {"id": "unlimited", "name": "Unlimited", "description": "For admins: no unit limits.",
     "features": list(FEATURES),
     "limits": {"questions_month": None, "questions_day": None, "notes_month": None, "notes_day": None,
                "words_month": None, "meeting_hours_month": None,
                "tokens_month": 30_000_000, "tokens_day": 3_000_000}},
]

ROLE_PLAN = {"viewer": "reader", "member": "contributor", "admin": "unlimited"}


def plan_for_new_user(role: str) -> str:
    return ROLE_PLAN.get(role, "reader")


def validate(features: list[str], limits: dict[str, Any]) -> None:
    """Raise ValueError for an unknown feature or limit, or a negative limit."""
    unknown = [f for f in features if f not in FEATURES]
    if unknown:
        raise ValueError(f"Unknown feature(s): {', '.join(unknown)}.")
    for key, value in limits.items():
        if key not in LIMITS:
            raise ValueError(f"Unknown limit: {key}.")
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{LIMITS[key]} must be a whole number of 0 or more, or empty for no limit.")


# ---------------------------------------------------------------- what needs what

def route_feature(method: str, path: str) -> str | None:
    """The feature an API request needs, or None when plans do not apply.

    Account, admin, pipeline and diagnostics routes are not plan features:
    roles already decide them, and they cost nothing a plan should meter.
    """
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path == "/ask":
        return "ask"
    if path == "/workspaces/search":
        return "cross_workspace"
    if path.startswith(("/graph", "/entities", "/paths")):
        return "graph"
    if path == "/notes" or path.startswith("/notes/"):
        return "browse" if method in ("GET", "HEAD") else "add_notes"
    if path.startswith("/ingest/"):
        # Reading a processed meeting is browsing; uploading, reprocessing,
        # deleting or correcting one is the Meetings feature.
        return "browse" if method in ("GET", "HEAD") else "meetings"
    return None


# MCP tools that need more than the connection itself. Every other tool reads,
# which the "mcp" feature (checked on connecting) already covers.
TOOL_FEATURES = {
    "brahmastra_add_note": "add_notes",
    "brahmastra_search_all_workspaces": "cross_workspace",
}


def refusal(feature: str) -> str:
    """What a person is told when their plan does not include a feature."""
    return f"{FEATURES.get(feature, feature)} is not in your plan. Ask an admin for an upgrade."
