# Brahmastra for AI agents

Gives your agent the team's shared memory: it recalls what was decided, tried and found broken before it starts, and stores decisions and root causes as it works. Brahmastra is a tool inside your project; your project's own CLAUDE.md or AGENTS.md stays in charge.

## What you need

- A Brahmastra install: a deployed one (`https://brahmastra.example.com/v1`) or a backend on your machine (`http://localhost:8001`) with remote MCP turned on (`MCP_HTTP=1`).
- A personal API token: in the dashboard, **Account > API tokens**. A local install with `BRAHMASTRA_ALLOW_ANONYMOUS=1` needs none.
- The workspace to use, usually one per project or team.

## Claude Code

```
/plugin marketplace add ShaanKapoor10/Brahmastra-KnowledgeGraph
/plugin install brahmastra@brahmastra
```

Claude Code asks for the URL, the token (stored in your system's credential store, never in a settings file) and the workspace. To change them later: `/plugin configure brahmastra@brahmastra`.

The plugin adds:

| Part | What it does |
|---|---|
| MCP server | The `brahmastra_*` tools, the `recall`, `remember` and `onboard` prompts, and the `brahmastra://guide` resource |
| Skill `brahmastra:brahmastra` | When to use the memory, and how to connect when the tools are missing |
| Hooks | On every request, the notes matching it are put in front of the model; when the agent stops, it is reminded to store a decision, root cause or declined request it settled; after a compaction, the briefing comes back |

The hooks are what make the memory get used. Measured in `evals/agent`: with the server alone, agents almost never looked, and went ahead with a change the team had decided against. With the hooks, they declined it and stored the new decisions, and only those.

## Codex, Cursor, Gemini CLI and other agents

The skill installs into any agent that reads Agent Skills:

```
npx skills add ShaanKapoor10/Brahmastra-KnowledgeGraph --skill brahmastra
```

Then connect the MCP server in that client; the skill's "When they are not available" section has the configuration for each. Everything about using the memory comes from the server itself, so the skill stays short and never goes stale.

## How the layers fit

| Layer | Owner | Holds |
|---|---|---|
| This plugin and the server | Brahmastra | How to use the memory. The same in every project |
| CLAUDE.md / AGENTS.md | Your project | Your code's rules. Wins any conflict |
| The workspace's `briefing` note | Your team | Shared context no repository holds: scope, owners, agreements, current focus |
| Your client settings | You | Personal preferences |
