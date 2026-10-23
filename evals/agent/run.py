"""
Do agents actually use Brahmastra on their own, and does it change the work?

Real headless Claude Code sessions do small tasks in an invented project
(fixture/larkspur, with its own CLAUDE.md) while a THROWAWAY Brahmastra holds
that team's memory (fixture/memory.json). Three setups:

    none     no Brahmastra at all                 the baseline
    server   the MCP server only                  what any MCP client gets
    skill    the plugin with its hooks removed    what the skill adds over the server
    plugin   plugins/brahmastra: server + skill + hooks (recall on each prompt,
             a store check when the agent stops)

Measured from each session's own event stream, never from what it says:
    recalled  a Brahmastra read before its first edit (or at all, if it edited nothing)
    stored    brahmastra_add_note called
    outcome   the task's own check on the files it left (see TASKS)

Isolation, because a bare Brahmastra process reads backend/.env and writes to
whatever it names: the server here runs with BRAHMASTRA_NO_DOTENV=1, a temp
SQLite file, no Notion and no model keys, and the run REFUSES to start unless
/health/ready says sqlite and the seeded notes are found in that temp file.
Sessions load no user or project settings (so no hooks), persist nothing, and
may use only file tools, `python -m pytest`, and Brahmastra's own tools.

    python evals/agent/run.py [--model haiku] [--runs 2] [--only none,server,plugin]
Costs one session per task x setup x run on your Claude login; ~1 minute each.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
PORT = 8031
URL = f"http://localhost:{PORT}"

READS = ("get_briefing", "search_notes", "search_entities", "get_entity_details",
         "search_sessions", "get_contradictions", "get_graph_stats", "search_all_workspaces")
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WEB_AND_AGENTS = ("WebFetch,WebSearch,Agent,Artifact,ArtifactComments,ArtifactData,"
                  "CronCreate,CronDelete,CronList,RemoteTrigger,PushNotification,SendMessage,"
                  "ScheduleWakeup,EnterWorktree,ExitWorktree,DesignSync,PowerShell")


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def _flaky_fixed_right(repo: Path, text: str) -> bool:
    """Fixed in the TEST, with UTC; due_today left in UTC (the stored root cause)."""
    core, test = _read(repo, "larkspur/core.py"), _read(repo, "tests/test_core.py")
    body = core.split("def due_today", 1)[-1].split("\ndef ", 1)[0]
    return "timezone.utc" in body and "timezone.utc" in test.split("def test_due_today", 1)[-1]


def _due_kept(repo: Path, text: str) -> bool:
    """The rename would break the mobile app; the decision says keep `due`."""
    core = _read(repo, "larkspur/core.py")
    return re.search(r"^\s+due: Optional\[str\]", core, re.M) is not None and '"due"' in core


def _py310(repo: Path, text: str) -> bool:
    return ">=3.10" in _read(repo, "pyproject.toml")


def _overdue(repo: Path, text: str) -> bool:
    return "def overdue" in _read(repo, "larkspur/core.py") and "overdue" in _read(repo, "larkspur/__init__.py")


# (id, what the user asks, outcome check, is storing expected)
TASKS = [
    ("flaky", "test_due_today fails on CI some nights but passes when I rerun it. Please fix it.",
     _flaky_fixed_right, False),
    ("rename", "Rename the Task field `due` to `due_at` everywhere, it reads better.",
     _due_kept, False),
    ("py310", "We're dropping Python 3.9: set requires-python to >=3.10. Reason: 3.9 is end-of-life "
              "and we want match statements in the reminders code.",
     _py310, True),
    ("overdue", "Add a function overdue(tasks, now=None) to larkspur that returns unfinished tasks "
                "whose due time has passed.",
     _overdue, False),
]


# ---------------------------------------------------------------------------
# The throwaway Brahmastra
# ---------------------------------------------------------------------------

def start_server(tmp: Path) -> tuple[subprocess.Popen, Path]:
    db = tmp / "eval.db"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "NEO4J_", "NOTION_", "GROQ_", "OPENAI_", "GEMINI_",
                                "OPENCODE_", "DATABASE_URL", "BRAHMASTRA_", "LLM_", "OLLAMA_"))}
    env.update({
        "BRAHMASTRA_NO_DOTENV": "1", "GRAPH_BACKEND": "sqlite", "NOTE_BACKEND": "",
        "BRAHMASTRA_DB": str(db), "BRAHMASTRA_DATA_DIR": str(tmp / "data"),
        "MCP_HTTP": "1", "BRAHMASTRA_ALLOW_ANONYMOUS": "1", "RATE_LIMITS": "off",
        "GRAPH_AUTO_REFRESH": "0", "LLM_PROVIDER": "", "OLLAMA_HOST": "http://127.0.0.1:1",
        "EMBEDDINGS_ENABLED": "0", "LIVE_SYNC": "0",
    })
    python = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    log = open(tmp / "server.log", "w")
    proc = subprocess.Popen([str(python), "-m", "uvicorn", "main:app", "--port", str(PORT)],
                            cwd=BACKEND, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    for _ in range(60):
        time.sleep(2)
        try:
            ready = json.load(urllib.request.urlopen(f"{URL}/health/ready", timeout=5))
            break
        except Exception:                                   # noqa: BLE001
            if proc.poll() is not None:
                raise SystemExit(f"server exited; see {tmp / 'server.log'}")
    else:
        proc.kill()
        raise SystemExit("server did not come up")
    if ready.get("system_of_record") != "sqlite" or ready.get("backend") != "sqlite":
        proc.kill()
        raise SystemExit(f"REFUSING: the eval server is not on a throwaway store: {ready}")
    return proc, db


def seed(db: Path) -> int:
    notes = json.loads((HERE / "fixture" / "memory.json").read_text(encoding="utf-8"))["notes"]
    for n in notes:
        body = json.dumps({"id": n["id"], "title": n["title"], "content": n["content"],
                           "extraction_status": "done"}).encode()
        req = urllib.request.Request(f"{URL}/notes", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    with sqlite3.connect(db) as con:
        found = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    if found != len(notes):
        raise SystemExit(f"REFUSING: seeded {len(notes)} notes but the temp store holds {found}")
    return found


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def claude_exe() -> str:
    found = shutil.which("claude") or "claude"
    if os.name == "nt":
        # The .CMD wrapper goes through cmd.exe, which drops empty arguments
        # and shifts every flag after them. Call the executable itself.
        exe = Path(found).parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if exe.exists():
            return str(exe)
    return found


def setup_args(setup: str, tmp: Path) -> list[str]:
    base = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python -m pytest:*)"]
    if setup == "none":
        return ["--strict-mcp-config", "--allowedTools", ",".join(base)]
    if setup == "server":
        config = tmp / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"brahmastra": {"type": "http", "url": f"{URL}/mcp"}}}))
        return ["--strict-mcp-config", "--mcp-config", str(config),
                "--allowedTools", ",".join(base + ["mcp__brahmastra"])]
    plugin = tmp / setup
    if not plugin.exists():
        shutil.copytree(REPO / "plugins" / "brahmastra", plugin)
        manifest = plugin / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["userConfig"]["server_url"]["default"] = URL
        manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if setup == "skill":
            (plugin / "hooks" / "hooks.json").unlink()
    return ["--plugin-dir", str(plugin),
            "--allowedTools", ",".join(base + ["mcp__plugin_brahmastra_brahmastra"])]


def run_session(setup: str, task: tuple, run: int, model: str, tmp: Path) -> dict:
    task_id, prompt, check, store_expected = task
    work = tmp / "runs" / f"{setup}-{task_id}-{run}"
    repo = work / "larkspur"
    shutil.copytree(HERE / "fixture" / "larkspur", repo)
    cmd = [claude_exe(), "-p", "--model", model, "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--no-session-persistence",
           "--disallowed-tools", WEB_AND_AGENTS, *setup_args(setup, tmp)]
    started = time.time()
    try:
        env = {**os.environ, "MCP_CONNECTION_NONBLOCKING": "0"}
        out = subprocess.run(cmd, input=prompt, cwd=repo, capture_output=True, text=True, env=env,
                             encoding="utf-8", timeout=480).stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    (work / "events.jsonl").write_text(out, encoding="utf-8")

    calls: list[tuple[str, dict]] = []
    final = ""
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    calls.append((block.get("name", ""), block.get("input") or {}))
        if event.get("type") == "result":
            final = event.get("result") or ""
    names = [n for n, _ in calls]
    first_read = next((i for i, n in enumerate(names) if n.startswith("mcp__") and n.endswith(READS)), None)
    first_edit = next((i for i, n in enumerate(names) if n in EDITS), None)
    recalled = first_read is not None and (first_edit is None or first_read < first_edit)
    stores = [i for n, i in calls if n.endswith("brahmastra_add_note")]
    return {
        "setup": setup, "task": task_id, "run": run, "seconds": round(time.time() - started),
        "recalled": recalled, "stored": bool(stores), "store_expected": store_expected,
        "outcome": check(repo, final), "tools": names, "final": final[:600],
        "stored_notes": [{"title": s.get("title"), "content": (s.get("content") or "")[:400]} for s in stores],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--only", default="none,server,skill,plugin")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--tasks", default=",".join(t[0] for t in TASKS))
    args = ap.parse_args()
    setups = args.only.split(",")

    tmp = Path(tempfile.mkdtemp(prefix="brahmastra-agent-eval-"))
    proc, db = start_server(tmp)
    try:
        print(f"throwaway store: {db} ({seed(db)} notes)", flush=True)
        chosen = [t for t in TASKS if t[0] in args.tasks.split(",")]
        jobs = [(s, t, r) for s in setups for t in chosen for r in range(args.runs)]
        for s in setups:            # built once, before sessions race to build it
            setup_args(s, tmp)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            results = list(pool.map(lambda j: run_session(j[0], j[1], j[2], args.model, tmp), jobs))
    finally:
        proc.kill()
    (tmp / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")

    print(f"\nmodel={args.model} runs={args.runs}   results: {tmp / 'results.json'}\n")
    print("task".ljust(10) + "".join(f"{s:>24}" for s in setups))
    print("".ljust(10) + "".join(f"{'recall store outcome':>24}" for _ in setups))
    for task_id, *_ in [t for t in TASKS if t[0] in args.tasks.split(",")]:
        row = task_id.ljust(10)
        for s in setups:
            rs = [r for r in results if r["setup"] == s and r["task"] == task_id]
            cell = (f"{sum(r['recalled'] for r in rs)}/{len(rs)}  {sum(r['stored'] for r in rs)}/{len(rs)}  "
                    f"{sum(r['outcome'] for r in rs)}/{len(rs)}") if s != "none" else f"-  -  {sum(r['outcome'] for r in rs)}/{len(rs)}"
            row += f"{cell:>24}"
        print(row)


if __name__ == "__main__":
    sys.exit(main())
