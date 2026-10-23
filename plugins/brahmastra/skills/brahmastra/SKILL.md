---
name: brahmastra
description: Shared, persistent team memory over MCP (Brahmastra). Use when starting work on a project, to recall what was already decided, tried or found broken; when something surprising needs debugging; and to store decisions, root causes and constraints as you work. Also use when the user mentions Brahmastra, the team's memory or knowledge graph, or asks you to remember something.
---

# Brahmastra: the team's shared memory

Brahmastra keeps what a team and its agents learn (decisions and their reasons, root causes, constraints, who owns what) in a knowledge graph that outlives every session and is shared by every teammate's agent. It is a tool inside this project, not its rulebook: the user and the project's own instructions (CLAUDE.md, AGENTS.md, its docs) come first, and memory never overrides them.

## When the `brahmastra_*` tools are available

1. Before planning: call `brahmastra_get_briefing`, then `brahmastra_search_notes` on the task.
2. When something surprises you: search before you debug.
3. When you decide something, find a root cause or hit a constraint: `brahmastra_add_note`, in the same turn, with the reason.

The server explains everything else and stays current with the install, so ask it rather than relying on this file: its instructions arrive when it connects, and the `onboard` prompt or the resource `brahmastra://guide` holds the full guide.

## When they are not available

Help the user connect. Ask for three things: the install's URL (for example `https://brahmastra.example.com/v1`), a personal API token (dashboard: Account > API tokens), and the workspace to use. Never write the token into a file that git tracks; keep it in an environment variable or the client's secure storage. Then, for their client:

- **Claude Code**: `/plugin marketplace add ShaanKapoor10/Brahmastra-KnowledgeGraph`, then `/plugin install brahmastra@brahmastra`, which asks for all three. Or directly: `claude mcp add --transport http brahmastra <URL>/mcp --header "Authorization: Bearer <TOKEN>" --header "X-Brahmastra-Workspace: <WORKSPACE>"`.
- **Codex** (`~/.codex/config.toml`): a `[mcp_servers.brahmastra]` table with `url = "<URL>/mcp"`, `bearer_token_env_var = "BRAHMASTRA_TOKEN"` and `http_headers = { "X-Brahmastra-Workspace" = "<WORKSPACE>" }`.
- **Cursor** (`.cursor/mcp.json` or `~/.cursor/mcp.json`): `"brahmastra": { "url": "<URL>/mcp", "headers": { "Authorization": "Bearer ${env:BRAHMASTRA_TOKEN}", "X-Brahmastra-Workspace": "<WORKSPACE>" } }` under `mcpServers`.
- **Gemini CLI** (`settings.json`): `"brahmastra": { "httpUrl": "<URL>/mcp", "headers": { ... } }` under `mcpServers`, with the same two headers.

Then install the hooks for that client, which put the matching notes in front of the agent on every request and remind it to store what it settles (Claude Code's plugin includes them). Codex: `codex plugin marketplace add ShaanKapoor10/Brahmastra-KnowledgeGraph`, then install `brahmastra` from `/plugins`. Cursor and Gemini CLI: the packages `plugins/brahmastra-cursor` and `plugins/brahmastra-gemini` in that repository, each with a README. The hooks need Node, and read `BRAHMASTRA_URL`, `BRAHMASTRA_TOKEN` and `BRAHMASTRA_WORKSPACE` or `~/.brahmastra/agent.json`.

Client configuration formats change; if one of these is rejected, check that client's MCP documentation for its current form. After connecting, restart the session so the tools load, then call `brahmastra_get_briefing`.
