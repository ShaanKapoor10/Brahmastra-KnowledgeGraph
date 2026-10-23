"use client"

/**
 * Connect an agent: the exact setup for each AI client, filled in with this
 * install's address and the workspace the person picks. The address comes
 * from the backend (GET /auth/me/connect), because only it knows whether it
 * sits behind Caddy at PUBLIC_URL/v1 or answers on localhost:8001.
 *
 * The token is never put into a snippet. Clients read it from the
 * environment (BRAHMASTRA_TOKEN) or ask for it into secure storage, so what is
 * copied from here is safe to keep in a dotfile.
 */
import { useEffect, useMemo, useState } from "react"
import { Check, Copy, Plug } from "lucide-react"
import { authApi } from "@/lib/auth-api"
import { Section } from "@/components/panels/account-panel"
import { AGENT_CONFIG, CLIENTS, TOKEN_VAR, agentConfig, type ConnectInfo } from "@/lib/connect"

export function ConnectAgent() {
  const [info, setInfo] = useState<ConnectInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ws, setWs] = useState<string>("")
  const [clientId, setClientId] = useState(CLIENTS[0].id)

  useEffect(() => {
    let live = true
    authApi<ConnectInfo>("/me/connect").then(
      (out) => {
        if (!live) return
        setInfo(out)
        setWs(out.workspace ?? out.workspaces[0] ?? "default")
      },
      (e) => { if (live) setError((e as Error).message) },
    )
    return () => { live = false }
  }, [])

  const client = CLIENTS.find((c) => c.id === clientId) ?? CLIENTS[0]
  const snippet = useMemo(() => (info ? client.snippet({ ...info, ws }) : ""), [info, client, ws])

  return (
    <Section title="Connect an agent" icon={Plug}>
      {error && <p role="alert" className="text-sm text-destructive">Could not load the connection details: {error}</p>}
      {info && !info.enabled && (
        <p className="text-sm text-amber-300">
          Remote MCP is off on this install, so agents cannot connect yet. An admin turns it on with{" "}
          <code className="font-mono text-xs">MCP_HTTP=1</code>.
        </p>
      )}
      {info && info.enabled && !info.allowed && (
        <p className="text-sm text-amber-300">Your plan does not include AI assistants (MCP). Ask for a plan that does, above.</p>
      )}
      {info && info.enabled && info.allowed && (
        <>
          <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm text-muted-foreground">
            {info.needs_token && (
              <li>
                Create a token under <span className="text-foreground">API tokens</span> and set it as{" "}
                <code className="font-mono text-xs">{TOKEN_VAR}</code> in your environment. Never paste it into a file git tracks.
              </li>
            )}
            <li>Pick your agent and copy its setup.</li>
            <li>
              Restart the agent. With the hooks, its first request brings the workspace briefing; without them, ask it
              to call <code className="font-mono text-xs">brahmastra_get_briefing</code>.
            </li>
          </ol>

          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-muted-foreground">Address</span>
            <code className="font-mono text-xs text-foreground">{info.mcp_url}</code>
            {info.workspaces.length > 1 && (
              <label className="ml-auto flex items-center gap-2 text-muted-foreground">
                Workspace
                <select value={ws} onChange={(e) => setWs(e.target.value)}
                        className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground">
                  {info.workspaces.map((w) => <option key={w} value={w}>{w}</option>)}
                </select>
              </label>
            )}
          </div>

          <div role="tablist" aria-label="Agent" className="flex flex-wrap gap-1.5">
            {CLIENTS.map((c) => (
              <button key={c.id} role="tab" aria-selected={c.id === client.id} onClick={() => setClientId(c.id)}
                      className={`rounded-md border px-2.5 py-1 text-xs ${c.id === client.id
                        ? "border-primary/50 bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:bg-secondary"}`}>
                {c.label}
              </button>
            ))}
          </div>

          {/* Keyed by its text: another agent or workspace is a new box, so "Copied" never lingers. */}
          <Snippet key={snippet} where={client.where} text={snippet} />
          {client.note && <p className="text-xs text-muted-foreground">{client.note}</p>}

          {client.hooks && info && (
            <>
              <p className="pt-2 text-sm text-foreground">
                Then install the hooks, so the agent uses the memory without being asked.
              </p>
              <Snippet key={`h${snippet}`} where={client.hooks.where} text={client.hooks.snippet({ ...info, ws })} />
              {client.hooks.note && <p className="text-xs text-muted-foreground">{client.hooks.note}</p>}
              <p className="pt-2 text-xs text-muted-foreground">
                If the agent does not see your shell&apos;s variables (an editor opened from a desktop icon), the
                hooks read this file instead. Keep it private to your user account.
              </p>
              <Snippet key={`c${snippet}`} where={AGENT_CONFIG} text={agentConfig({ ...info, ws })} />
            </>
          )}
        </>
      )}
    </Section>
  )
}

function Snippet({ where, text }: { where: string; text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[11px] text-muted-foreground">{where}</span>
        <button onClick={() => navigator.clipboard.writeText(text).then(() => setCopied(true))}
                className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-secondary">
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-md border border-border bg-background p-3 font-mono text-xs leading-relaxed text-foreground">
        {text}
      </pre>
    </div>
  )
}
