# Brahmastra hooks for Codex

Brings the team's memory to Codex at the moments that matter, without the agent having to decide to look:

| Codex event | What Brahmastra adds |
|---|---|
| `UserPromptSubmit` | The stored notes matching the request; on a session's first request, the workspace briefing too |
| `Stop` | When the turn settled a decision, a root cause or a declined request: a reminder to store it, once |
| `SessionStart` (after compaction) | The briefing again |

Agents with only the MCP server almost never looked (measured in `evals/agent`); hooks are what change that.

## Install

1. Connect the MCP server so the agent can search and store. In `~/.codex/config.toml`:

   ```toml
   [mcp_servers.brahmastra]
   url = "https://brahmastra.example.com/v1/mcp"
   bearer_token_env_var = "BRAHMASTRA_TOKEN"
   http_headers = { "X-Brahmastra-Workspace" = "default" }
   ```

2. Add the plugin, which carries the hooks:

   ```
   codex plugin marketplace add ShaanKapoor10/Brahmastra-KnowledgeGraph
   ```

   In Codex, install `brahmastra` from `/plugins`, then review and trust its hooks in `/hooks`.

3. Tell the hooks where Brahmastra is: set `BRAHMASTRA_URL` (the address without `/mcp`, such as `https://brahmastra.example.com/v1` or `http://localhost:8001`), `BRAHMASTRA_TOKEN` and `BRAHMASTRA_WORKSPACE`. Alternatively, put them in `~/.brahmastra/agent.json` as `{"url": ..., "token": ..., "workspace": ...}`, readable only by you.

The hooks need Node 18 or later. They never fail a turn: if Brahmastra is unreachable they add nothing and say why on stderr.

The dashboard's **Account > Connect an agent** fills all of this in for your install.
