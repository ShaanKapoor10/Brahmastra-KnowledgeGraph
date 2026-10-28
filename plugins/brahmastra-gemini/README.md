# Brahmastra extension for Gemini CLI

Brings the team's memory to Gemini CLI without the agent having to decide to look:

| Gemini event | What Brahmastra adds |
|---|---|
| `SessionStart` | The workspace briefing |
| `BeforeAgent` | The stored notes matching the request, and, when the previous turn settled a decision or a root cause, a reminder to store it |
| `AfterAgent` | Nothing visible: the reply is checked for something settled |

The reminder comes at the start of the next turn rather than the end of this one, because Gemini's `AfterAgent` can only reject an answer and ask again, which would throw away a good reply.

## Install

```
git clone --depth 1 https://github.com/ShaanKapoor10/Brahmastra-KnowledgeGraph ~/.brahmastra/src
gemini extensions install ~/.brahmastra/src/plugins/brahmastra-gemini
```

Gemini asks for the address (without `/mcp`, such as `https://brahmastra.example.com/v1` or `http://localhost:8001`), a personal API token, which it keeps in your system keychain, and the workspace. `~/.brahmastra/agent.json` (`{"url": ..., "token": ..., "workspace": ...}`) works too.

Then connect the MCP server so the agent can search and store, in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "brahmastra": {
      "httpUrl": "https://brahmastra.example.com/v1/mcp",
      "headers": {
        "Authorization": "Bearer $BRAHMASTRA_TOKEN",
        "X-Brahmastra-Workspace": "default"
      }
    }
  }
}
```

The hooks need Node 18 or later. They never fail a turn: if Brahmastra is unreachable they add nothing. They send a token only over https, or plain http to this machine (`BRAHMASTRA_ALLOW_HTTP=1` allows a private network).
