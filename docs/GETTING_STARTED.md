# Getting started

This guide takes you from a fresh clone to an assistant that remembers. You need Docker,
Python 3.12 and an API key for a model provider. Groq is the default, and its free tier
is enough to try Brahmastra.

1. [Run the stack](#1-run-the-stack)
2. [Connect your assistant](#2-connect-your-assistant)
3. [Teach your assistant to use its memory](#3-teach-your-assistant-to-use-its-memory)
4. [Capture sessions automatically](#4-capture-sessions-automatically)
5. [Add your first meeting](#5-add-your-first-meeting)
6. [Connect Notion](#6-connect-notion)
7. [Organise with workspaces](#7-organise-with-workspaces)

---

## 1. Run the stack

```sh
git clone https://github.com/ShaanKapoor10/vaultgraph-qy.git
cd vaultgraph-qy

cat > .env <<'EOF'
GROQ_API_KEY=your-groq-key
BRAHMASTRA_ALLOW_ANONYMOUS=1
EOF

docker compose up -d --build
```

Run Compose from the repository root. That starts four services:

| service | what it does | address |
|---|---|---|
| `postgres` | stores your notes, with hybrid search | `localhost:5433` |
| `neo4j` | stores the knowledge graph | `localhost:7687` |
| `backend` | the API and the pipeline | <http://localhost:8001> |
| `frontend` | the dashboard | <http://localhost:3000> |

Check that it is ready:

```sh
curl http://localhost:8001/health/ready
```

`BRAHMASTRA_ALLOW_ANONYMOUS=1` opens the API without a key, which suits a single machine.
Without it, and without an API key, the API refuses all requests. For anything shared,
set `BRAHMASTRA_API_KEY` instead; see [Deployment](DEPLOYMENT.md).

**More than one Groq key?** Set `GROQ_API_KEYS` to a comma-separated list. Brahmastra
rotates between them as their limits run out. You can also add, test and disable keys
from the dashboard's Diagnostics tab once `BRAHMASTRA_KEY_ADMIN=1` is set.

**Another model provider?** Set `LLM_PROVIDER` to `openai`, `anthropic`, `gemini` or
`ollama`, along with that provider's key. Everything uses the selected provider:
extraction, answers, summaries and meetings.

**Keep the graph current automatically:**

```sh
docker compose --profile scheduler up -d
```

## 2. Connect your assistant

The MCP server runs on your machine and talks to your assistant over stdio. Install it
once:

```sh
cd backend
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

Give it a `backend/.env` that reaches the containers from your machine:

```sh
NOTE_BACKEND=postgres
GRAPH_BACKEND=neo4j
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=brahmastra
GROQ_API_KEY=your-groq-key
```

Then register it with your assistant. For Claude Code, add this to `.mcp.json` in your
project. Other MCP clients take the same block in their own settings.

```json
{
  "mcpServers": {
    "brahmastra": {
      "command": "/path/to/vaultgraph-qy/backend/.venv/bin/python",
      "args": ["-m", "brahmastra.mcp_server"],
      "cwd": "/path/to/vaultgraph-qy/backend"
    }
  }
}
```

On Windows, the interpreter is `backend\.venv\Scripts\python.exe`.

Restart your assistant, and twelve `brahmastra_*` tools appear. To check, ask it to call
`brahmastra_get_graph_stats`.

**Connecting from another machine?** Point that machine's `backend/.env` at the same
Postgres and Neo4j, and both assistants share one memory. To serve MCP over HTTP behind
the API's authentication instead, see [Deployment](DEPLOYMENT.md#using-mcp-against-a-deployed-instance).

## 3. Teach your assistant to use its memory

Connecting the tools makes them available. Your assistant still needs to be told when to
use them. Add this to your `CLAUDE.md`, or to your agent's system prompt:

```markdown
## Memory
Brahmastra is your long-term memory for this project.

- Before starting a task, call brahmastra_search_notes for what was already decided,
  tried or found broken.
- After a decision, a fix or a change, call brahmastra_add_note in the same turn.
  Write plain sentences that name things: "The file llm.py retries on a 429."
- To find where something is implemented, use brahmastra_search_code.
```

Notes written as clear sentences, with the names of people, files and projects in them,
turn into the richest graph.

## 4. Capture sessions automatically

Claude Code can hand each conversation to Brahmastra as it happens, so nothing is lost
when a session ends or its context is compacted. Add these hooks to
`.claude/settings.json`:

```json
{
  "hooks": {
    "Stop":       [{ "hooks": [{ "type": "command", "command": "<venv python> <repo>/backend/brahmastra/checkpoint.py" }] }],
    "PreCompact": [{ "hooks": [{ "type": "command", "command": "<venv python> <repo>/backend/brahmastra/checkpoint.py" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": "<venv python> <repo>/backend/brahmastra/checkpoint.py" }] }]
  }
}
```

On Windows, use `pythonw.exe` so no console window opens. Each session is distilled into
a verified note, and the full conversation is indexed for `brahmastra_search_sessions`.
To see what is queued:

```sh
python -m brahmastra.checkpoint --status
```

## 5. Add your first meeting

Open the dashboard at <http://localhost:3000>, go to **Meetings** and upload a transcript.
It accepts plain `Speaker: text` lines, SRT or WebVTT, including Teams exports. Choose
**Meeting** or **Lecture**, then pick a workspace. Progress shows part by part as the
transcript is read.

The same thing works from the API:

```sh
curl -F "file=@standup.vtt" -F "mode=meeting" -F "title=Q3 release sync" \
     "http://localhost:8001/ingest/transcripts/upload?workspace=default"
```

When it finishes, each decision, action item, risk and open question appears with its
speaker, quote, time and status. The session is also in the graph, so your assistant can
answer "what did we decide about the export API?".

## 6. Connect Notion

Create a Notion integration, share your pages or database with it, and add to `.env`:

```sh
NOTION_TOKEN=your-notion-token
NOTION_DATABASE_ID=your-database-or-page-id
```

Each pipeline run pulls changed pages in and writes a **Brahmastra Insights** toggle back
onto each page, with relationships, contradictions and suggested links. A workspace can
name its own Notion source, so a work graph can sync from a work Notion.

## 7. Organise with workspaces

Workspaces keep separate graphs apart: one for personal notes, one for work, one per
project. Create them from the dashboard, from the API (`POST /workspaces`), or by asking
your assistant to call `brahmastra_create_workspace`. Choose one per process with
`BRAHMASTRA_WORKSPACE`, or per request with `?workspace=`.

---

**Next:** [How it works](HOW_IT_WORKS.md) for the architecture, or
[Deployment](DEPLOYMENT.md) to run it for a team.
