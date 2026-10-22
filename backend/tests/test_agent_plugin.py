"""
The agent plugin (plugins/brahmastra) and the server it talks to must agree.

The plugin is JSON and Markdown that nothing executes in this suite, so a tool
renamed in mcp_server.py, a userConfig key renamed in plugin.json, or a skill
that grew a Claude-only field would ship broken with every test green. These
check the joins. `claude plugin validate` checks the formats themselves.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "brahmastra"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _skill() -> tuple[dict[str, str], str]:
    # Normalised: a Windows checkout turns the file's line endings into CRLF.
    text = (PLUGIN / "skills" / "brahmastra" / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert match, "SKILL.md needs YAML frontmatter"
    fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in fields.items()}, match.group(2)


def test_the_marketplace_lists_the_plugin_where_it_is():
    market = _json(REPO / ".claude-plugin" / "marketplace.json")
    entry = next(p for p in market["plugins"] if p["name"] == "brahmastra")
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()
    assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["name"] == entry["name"]


def test_the_server_config_uses_only_declared_options():
    """An undeclared ${user_config.X} fails to load in Claude Code."""
    declared = set(_json(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"])
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", (PLUGIN / ".mcp.json").read_text()))
    assert used and used <= declared, used - declared


def test_the_token_is_never_stored_in_plain_settings():
    options = _json(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]
    assert options["api_token"].get("sensitive") is True


def test_the_workspace_header_is_the_one_the_server_reads():
    pytest.importorskip("mcp", reason="mcp SDK is installed in backend/.venv only")
    from brahmastra.mcp_server import WORKSPACE_HEADER

    headers = _json(PLUGIN / ".mcp.json")["mcpServers"]["brahmastra"]["headers"]
    assert WORKSPACE_HEADER in headers


def test_the_skill_stays_portable():
    """Gemini CLI reads only name and description and drops the rest silently,
    so anything else would be a Claude-only behaviour in a cross-agent file."""
    fields, body = _skill()
    assert set(fields) == {"name", "description"}
    assert fields["name"] == "brahmastra"
    assert len(fields["description"]) <= 1024
    assert "CLAUDE.md" in body and "come first" in body, "the precedence rule travels with the skill"


def test_the_plugin_names_only_real_tools():
    pytest.importorskip("mcp", reason="mcp SDK is installed in backend/.venv only")
    from brahmastra import mcp_server

    real = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    _, body = _skill()
    for name in re.findall(r"brahmastra_\w+", body):
        assert name in real, f"SKILL.md names {name}, which the server does not serve"
    for event in _json(PLUGIN / "hooks" / "hooks.json")["hooks"].values():
        for group in event:
            for hook in group["hooks"]:
                if hook["type"] == "mcp_tool":
                    assert hook["tool"] in real, hook["tool"]
                    assert hook["server"] == "plugin:brahmastra:brahmastra"


def test_the_briefing_hook_runs_after_compaction_not_at_launch():
    """At launch SessionStart fires before MCP servers connect, so an mcp_tool
    hook there is skipped. After a compaction it runs, and that is when a
    long session has drifted furthest from the rules (arXiv 2605.10039)."""
    groups = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]["SessionStart"]
    assert [g["matcher"] for g in groups] == ["compact"]
