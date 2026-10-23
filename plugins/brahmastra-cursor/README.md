# Brahmastra hooks for Cursor

Brings the team's memory to Cursor's agent without it having to decide to look:

| Cursor event | What Brahmastra adds |
|---|---|
| `sessionStart` | The workspace briefing |
| `beforeSubmitPrompt`, `afterAgentResponse` | Nothing visible: the request and reply are noted for the check below |
| `stop` | When the turn settled a decision, a root cause or a declined request: one follow-up message asking the agent to store it |

Cursor cannot add context to an individual request, so unlike Claude Code, Codex and Gemini CLI, the notes matching each request are not put in front of the agent; it searches them itself through the MCP tools.

## Install

1. Connect the MCP server. In `~/.cursor/mcp.json`:

   ```json
   {
     "mcpServers": {
       "brahmastra": {
         "url": "https://brahmastra.example.com/v1/mcp",
         "headers": {
           "Authorization": "Bearer ${env:BRAHMASTRA_TOKEN}",
           "X-Brahmastra-Workspace": "default"
         }
       }
     }
   }
   ```

2. Install the plugin, which carries the hooks:

   ```
   git clone --depth 1 https://github.com/ShaanKapoor10/Brahmastra-KnowledgeGraph ~/.brahmastra/src
   mkdir -p ~/.cursor/plugins/local
   cp -r ~/.brahmastra/src/plugins/brahmastra-cursor ~/.cursor/plugins/local/brahmastra
   ```

3. Tell the hooks where Brahmastra is. Cursor opened from a desktop icon does not see your shell's variables, so use `~/.brahmastra/agent.json`, readable only by you:

   ```json
   {"url": "https://brahmastra.example.com/v1", "token": "<your token>", "workspace": "default"}
   ```

   `BRAHMASTRA_URL`, `BRAHMASTRA_TOKEN` and `BRAHMASTRA_WORKSPACE` work too, and win over the file.

4. Restart Cursor.

The hooks need Node 18 or later. They never fail a turn: if Brahmastra is unreachable they add nothing.
