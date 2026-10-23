#!/usr/bin/env node
// Brahmastra agent hook: hands the client's hook input to Brahmastra and
// prints Brahmastra's answer, in that client's own format.
//
//   node brahmastra-hook.mjs <codex|cursor|gemini|claude>
//
// Every rule lives on the server (backend/brahmastra/agent_hooks.py), so this
// file is a pipe and is the same in every plugin; tests enforce identical
// copies. It never fails the agent's turn: on any error it prints nothing,
// says why on stderr and exits 0.
//
// Configuration, from the environment:
//   BRAHMASTRA_URL        https://brahmastra.example.com/v1, or http://localhost:8001
//   BRAHMASTRA_TOKEN      a personal API token (Account > API tokens)
//   BRAHMASTRA_WORKSPACE  the workspace to read, if not the default
// or, for an editor started from a desktop shortcut (which does not see the
// shell's variables), from ~/.brahmastra/agent.json:
//   {"url": "...", "token": "...", "workspace": "..."}
// A variable that is set wins over the file.

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

let file = {};
try {
  file = JSON.parse(readFileSync(join(homedir(), ".brahmastra", "agent.json"), "utf8"));
} catch {
  // No file is the normal case when the variables are set.
}
const setting = (name, key) => process.env[name] || file[key] || "";

const client = process.argv[2] || "claude";
const base = (setting("BRAHMASTRA_URL", "url") || "http://localhost:8001").replace(/\/+$/, "");
const token = setting("BRAHMASTRA_TOKEN", "token");
const workspace = setting("BRAHMASTRA_WORKSPACE", "workspace");
const timeoutMs = Number(process.env.BRAHMASTRA_HOOK_TIMEOUT_MS) || 8000;

const fail = (why) => {
  process.stderr.write(`brahmastra hook: ${why}\n`);
  process.exit(0);
};

let input = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) input += chunk;

const headers = { "Content-Type": "application/json" };
if (token) headers.Authorization = `Bearer ${token}`;
if (workspace) headers["X-Brahmastra-Workspace"] = workspace;

let response;
try {
  response = await fetch(`${base}/hooks?client=${encodeURIComponent(client)}`, {
    method: "POST",
    headers,
    body: input.trim() || "{}",
    signal: AbortSignal.timeout(timeoutMs),
  });
} catch (error) {
  fail(`${base} unreachable (${error.cause?.code || error.name})`);
}
if (!response.ok) fail(`${base} answered ${response.status}`);

const output = (await response.text()).trim();
if (output && output !== "{}") process.stdout.write(output);
process.exit(0);
