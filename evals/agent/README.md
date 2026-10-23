# Do agents use Brahmastra on their own?

`run.py` gives real headless Claude Code sessions small tasks in an invented project, Larkspur, while a throwaway Brahmastra holds that team's memory. It answers two questions: does the agent reach for the memory without being told to, and does the memory change the result?

```
python evals/agent/run.py --model haiku --runs 2                 # every setup
python evals/agent/run.py --model sonnet --runs 1 --only server,plugin
```

Each session costs one Claude Code run on your login (about a minute). The run refuses to start unless its server reports a SQLite store in a temp directory and the seeded notes are found there, so it can never touch a real memory.

## The fixture

`fixture/larkspur` is a small todo library with its own `CLAUDE.md`. `fixture/memory.json` is its team's memory: a briefing, two pieces of history that matter to the tasks, and two unrelated notes. Every person, project and decision in it is invented.

| Task | What the user asks | Right outcome | Needs memory? |
|---|---|---|---|
| `rename` | Rename `Task.due` to `due_at` | Decline or ask: the stored decision says the mobile app parses `due`, and Tomas Weil signs off renames | Yes |
| `flaky` | Fix a test that fails on CI some nights | Fix the test with UTC and leave `due_today` in UTC | Partly: the root cause is stored |
| `py310` | Drop Python 3.9, and why | Update `pyproject.toml` and **store the decision** | Stores |
| `overdue` | Add `overdue()` | The function, exported | No |

## Setups

| Setup | What the session has |
|---|---|
| `none` | No Brahmastra |
| `server` | The MCP server only: its instructions, tools, prompts and guide |
| `skill` | The plugin with its hooks removed: server plus the `brahmastra` skill |
| `plugin` | `plugins/brahmastra` as shipped: server, skill and hooks |

## Results (2026-10-23)

Haiku, 2 runs per cell; `rename` and `py310` are the two that need memory:

| | none | server | skill | plugin |
|---|---|---|---|---|
| `rename`: kept `due` | 0/2 | 0/2 | 0/2 | **2/2** |
| `py310`: decision stored | n/a | 0/2 | 0/2 | **2/2** |
| `overdue`: nothing stored (routine) | n/a | 2/2 | 2/2 | 2/2 |
| All four outcomes | 5/8 | 5/8 | 6/8 | **8/8** |
| Recalled by its own choice | n/a | 1/16 over two runs | 0/8 | 0/8 |

Sonnet, 1 run: `server` renamed the field and stored nothing; `plugin` declined the rename and stored the Python 3.9 decision.

**Plugin 0.3.0** (hook rules moved to `agent_hooks.py`, briefing on a session's first request), Haiku, 2 runs: the same 8/8 outcomes, `rename` kept `due` 2/2, `py310` stored 2/2, `overdue` stored nothing 2/2. New: one `flaky` session of two stored a note restating the root cause that memory already held, although the reminder says to skip what is already stored. One unneeded note in 8 sessions; watch it on the next run before changing the reminder.

**What this says:**

1. **Agents do not reach for memory on their own,** whatever the instructions say. Across 24 server and skill sessions, one looked. That is the failure that made this project's own CLAUDE.md demand recall in capitals, measured.
2. **The skill alone changed nothing measurable:** neither model invoked it in 8 sessions. It stays, because it carries the setup instructions for agents without Brahmastra connected, but it is not what makes memory used.
3. **Hooks do.** The plugin puts recall in front of the model on every request (`UserPromptSubmit`) and checks for unstored decisions when it stops (`Stop`). That turned 0/2 into 2/2 on the task that needs memory, and stored the decision that needed storing, without storing routine work or what was already known.

## What went wrong on the way, and stays fixed

- **Claude Code gives an MCP hook's result to the model only as JSON** with `hookSpecificOutput.additionalContext`. Plain text, which every other tool returns, arrives as the word "completed". The first recall hook fetched the right notes and the agent never saw them. Hence `brahmastra_recall`, a tool shaped for hooks.
- **A prompt-type Stop hook errored**, and Claude Code then showed the agent the hook's own instructions. The agent answered `{"ok": true}` as its final reply, and the stores it made were accidents. The Stop check is now deterministic: the hook passes the agent's last message, and the server adds one reminder when that or the turn's request reads like something was settled.
- **The reason for a decision is often only in the request.** The reply says "Done, updated pyproject.toml". So the server remembers each session's last request, and the Stop check reads both.
- **Harness bugs:**
  - The first run disallowed the `Skill` tool, so the skill could never load.
  - Parallel sessions raced to build the same plugin copy.
  - Both are fixed. The first result table was discarded rather than reported.

## Reading a run

`results.json` and each session's `events.jsonl` are in the temp directory the run prints. **Recalled** means a Brahmastra read tool was called before the first edit. Hook-delivered recall is not counted there, which is why it reads 0/8 for the plugin while the outcome changed: the memory reached the model without the model asking for it.
