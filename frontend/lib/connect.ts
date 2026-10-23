/**
 * The setup each AI client needs to reach a Brahmastra install over MCP.
 * Pure functions of GET /auth/me/connect, so they can be checked without a browser.
 * The token is never written into a snippet: clients read it from the environment.
 */
export type ConnectInfo = {
  enabled: boolean
  allowed: boolean
  needs_token: boolean
  server_url: string
  mcp_url: string
  workspace_header: string
  workspaces: string[]
  workspace: string | null
  marketplace: string
  plugin: string
  skill: string
}

/** A second step that installs the agent's hooks: the part that makes it use the memory unasked. */
export type Hooks = { where: string; note?: string; snippet: (c: Filled) => string }
export type Client = {
  id: string
  label: string
  where: string
  note?: string
  snippet: (c: Filled) => string
  hooks?: Hooks
}
export type Filled = ConnectInfo & { ws: string }

export const TOKEN_VAR = "BRAHMASTRA_TOKEN"
export const AGENT_CONFIG = "~/.brahmastra/agent.json"
export const SOURCE = "~/.brahmastra/src"

const clone = (c: Filled) => `git clone --depth 1 https://github.com/${c.marketplace} ${SOURCE}`

/** What the hook script reads when the editor does not see your shell's variables. */
export function agentConfig(c: Filled): string {
  return JSON.stringify(
    { url: c.server_url, workspace: c.ws, ...(c.needs_token ? { token: "<your token>" } : {}) },
    null,
    2,
  )
}

export const CLIENTS: Client[] = [
  {
    id: "claude-plugin",
    label: "Claude Code",
    where: "In a Claude Code session",
    note: "The plugin asks for the address, your token and the workspace, and keeps the token in your system's credential store. Its hooks bring the matching notes to every request and remind the agent to store decisions.",
    snippet: (c) =>
      [
        `/plugin marketplace add ${c.marketplace}`,
        `/plugin install ${c.plugin}`,
        "",
        `# When it asks:  URL ${c.server_url}   workspace ${c.ws}` + (c.needs_token ? "   token <your token>" : ""),
      ].join("\n"),
  },
  {
    id: "claude-cli",
    label: "Claude Code (one command)",
    where: "In your terminal",
    snippet: (c) =>
      [
        `claude mcp add --transport http brahmastra ${c.mcp_url}`,
        ...(c.needs_token ? [`  --header "Authorization: Bearer $${TOKEN_VAR}"`] : []),
        `  --header "${c.workspace_header}: ${c.ws}"`,
      ].join(" \\\n"),
  },
  {
    id: "codex",
    label: "Codex",
    where: "~/.codex/config.toml",
    snippet: (c) =>
      [
        "[mcp_servers.brahmastra]",
        `url = "${c.mcp_url}"`,
        ...(c.needs_token ? [`bearer_token_env_var = "${TOKEN_VAR}"`] : []),
        `http_headers = { "${c.workspace_header}" = "${c.ws}" }`,
      ].join("\n"),
    hooks: {
      where: "In your terminal, then in Codex",
      note: "In Codex, install brahmastra from /plugins and trust its hooks in /hooks. The hooks need Node.",
      snippet: (c) => `codex plugin marketplace add ${c.marketplace}`,
    },
  },
  {
    id: "cursor",
    label: "Cursor",
    where: "~/.cursor/mcp.json",
    note: "Cursor started from a desktop icon may not see variables set in your shell profile; set the token in your system environment and restart Cursor.",
    snippet: (c) =>
      JSON.stringify(
        {
          mcpServers: {
            brahmastra: {
              url: c.mcp_url,
              headers: {
                ...(c.needs_token ? { Authorization: `Bearer \${env:${TOKEN_VAR}}` } : {}),
                [c.workspace_header]: c.ws,
              },
            },
          },
        },
        null,
        2,
      ),
    hooks: {
      where: "In your terminal",
      note: "Restart Cursor. Cursor cannot add memory to each request, so it gets the briefing when a chat opens and a reminder to store decisions when the agent finishes.",
      snippet: (c) =>
        [
          clone(c),
          "mkdir -p ~/.cursor/plugins/local",
          `cp -r ${SOURCE}/plugins/brahmastra-cursor ~/.cursor/plugins/local/brahmastra`,
        ].join("\n"),
    },
  },
  {
    id: "gemini",
    label: "Gemini CLI",
    where: "~/.gemini/settings.json",
    snippet: (c) =>
      JSON.stringify(
        {
          mcpServers: {
            brahmastra: {
              httpUrl: c.mcp_url,
              headers: {
                ...(c.needs_token ? { Authorization: `Bearer $${TOKEN_VAR}` } : {}),
                [c.workspace_header]: c.ws,
              },
            },
          },
        },
        null,
        2,
      ),
    hooks: {
      where: "In your terminal",
      note: "Gemini asks for the address, token and workspace while installing. The hooks need Node.",
      snippet: (c) => [clone(c), `gemini extensions install ${SOURCE}/plugins/brahmastra-gemini`].join("\n"),
    },
  },
  {
    id: "skill",
    label: "Skill (any agent)",
    where: "In your terminal",
    note: "Teaches the agent when to use the memory and how to connect. Pair it with one of the MCP setups above.",
    snippet: (c) => `npx skills add ${c.marketplace} --skill ${c.skill}`,
  },
]
