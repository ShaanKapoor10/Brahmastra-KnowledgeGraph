"use client"

/**
 * Your own account: change your password, and create or revoke personal API
 * tokens for MCP clients and scripts. A token is shown ONCE, when created; the
 * server keeps only its hash, so it cannot be shown again.
 */
import { useEffect, useState } from "react"
import { BadgeCheck, Check, Copy, KeyRound, Loader2, Lock, Plus, Trash2 } from "lucide-react"
import { authApi, when, type ApiToken } from "@/lib/auth-api"
import type { PlanCatalog, UsageRow } from "@/lib/plans"
import { MyRequests } from "@/components/panels/plan-requests"
import { ConnectAgent } from "@/components/panels/connect-agent"
import type { SignedInUser } from "@/components/dashboard"

export function AccountPanel({ user }: { user: SignedInUser }) {
  if (user.kind !== "session") {
    return (
      <div className="flex flex-col gap-4">
        <p className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
          You are signed in with {user.kind === "anonymous" ? "development mode" : "the service key"}, which has no
          account to manage. Sign in as a person to change a password or create API tokens.
        </p>
        <ConnectAgent />
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-4">
      <header>
        <h2 className="text-base font-semibold text-foreground">{user.name || user.email}</h2>
        <p className="text-sm text-muted-foreground">
          {user.email} · {user.role}
        </p>
      </header>
      <YourPlan planId={user.plan ?? null} />
      <MyRequests planId={user.plan ?? null} />
      <Tokens />
      <ConnectAgent />
      <ChangePassword />
    </div>
  )
}

function YourPlan({ planId }: { planId: string | null }) {
  const [catalog, setCatalog] = useState<PlanCatalog | null>(null)
  useEffect(() => {
    let live = true
    authApi<PlanCatalog>("/plans").then((c) => { if (live) setCatalog(c) }, () => {})
    return () => { live = false }
  }, [])
  const [usage, setUsage] = useState<UsageRow[] | null>(null)
  useEffect(() => {
    let live = true
    authApi<{ usage: UsageRow[] }>("/usage").then((u) => { if (live) setUsage(u.usage) }, () => {})
    return () => { live = false }
  }, [])
  const plan = catalog?.plans.find((p) => p.id === planId)
  if (!catalog || !plan) return null
  return (
    <Section title={`Your plan: ${plan.name}`} icon={BadgeCheck}>
      {plan.description && <p className="text-sm text-muted-foreground">{plan.description}</p>}
      <ul className="grid gap-1 sm:grid-cols-2">
        {Object.entries(catalog.features).map(([id, name]) => {
          const on = plan.features.includes(id)
          return (
            <li key={id} className={`flex items-center gap-1.5 text-sm ${on ? "text-foreground" : "text-muted-foreground"}`}>
              {on ? <Check className="h-3.5 w-3.5 text-primary" /> : <Lock className="h-3.5 w-3.5" />} {name}
            </li>
          )
        })}
      </ul>
      {usage && usage.length > 0 && (
        <div className="flex flex-col gap-2.5">
          {usage.map((row) => (
            <UsageBar key={row.limit_key} row={row} label={catalog.limits[row.limit_key] ?? row.limit_key} />
          ))}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Daily limits reset at midnight, monthly ones on the 1st. Need more? Ask below.
      </p>
    </Section>
  )
}

function UsageBar({ row, label }: { row: UsageRow; label: string }) {
  const unlimited = row.limit == null
  const share = unlimited || row.limit === 0 ? (row.used > 0 ? 1 : 0) : Math.min(1, row.used / row.limit!)
  // Not in the plan at all (a limit of 0) reads differently from "used up".
  const state = row.limit === 0 ? "none" : share >= 1 ? "full" : share >= 0.8 ? "near" : "ok"
  const tone = { ok: "bg-primary", near: "bg-amber-400", full: "bg-destructive", none: "bg-muted" }[state]
  const shown = (n: number) => (Number.isInteger(n) ? n.toLocaleString() : n.toFixed(1))
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono tabular-nums text-foreground">
          {unlimited ? `${shown(row.used)} · no limit` : row.limit === 0 ? "not in your plan" : `${shown(row.used)} / ${shown(row.limit!)}`}
        </span>
      </div>
      {!unlimited && row.limit !== 0 && (
        <div className="h-1.5 overflow-hidden rounded-full bg-secondary" role="progressbar"
             aria-label={label} aria-valuemin={0} aria-valuemax={row.limit!} aria-valuenow={row.used}>
          <div className={`h-full ${tone}`} style={{ width: `${Math.round(share * 100)}%` }} />
        </div>
      )}
      {state === "near" && <p className="text-xs text-amber-300">Over 80% used.</p>}
      {state === "full" && <p className="text-xs text-destructive">Used up until it resets.</p>}
    </div>
  )
}

function Section({ title, icon: Icon, children }: { title: string; icon: React.ElementType; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
      <h3 className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
        <Icon className="h-3.5 w-3.5" /> {title}
      </h3>
      {children}
    </section>
  )
}

export function Notice({ message }: { message: { tone: "ok" | "error"; text: string } | null }) {
  if (!message) return null
  return (
    <p role={message.tone === "error" ? "alert" : "status"}
       className={`text-sm ${message.tone === "error" ? "text-destructive" : "text-green-400"}`}>
      {message.text}
    </p>
  )
}

export function CopyOnce({ label, secret }: { label: string; secret: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
      <p className="text-sm text-amber-200">{label}</p>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto rounded bg-background px-2 py-1 font-mono text-xs text-foreground">
          {secret}
        </code>
        <button
          onClick={() => navigator.clipboard.writeText(secret).then(() => setCopied(true))}
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-secondary"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  )
}

const inputClass =
  "rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
const buttonClass =
  "flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-sm text-primary hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-50"

function Tokens() {
  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [name, setName] = useState("")
  const [created, setCreated] = useState<{ name: string; token: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null)

  // Bumped after every change; the effect below reloads on it.
  const [version, setVersion] = useState(0)
  const load = () => setVersion((v) => v + 1)
  useEffect(() => {
    let live = true            // a reply for an unmounted panel is dropped
    authApi<{ tokens: ApiToken[] }>("/tokens").then(
      (out) => { if (live) setTokens(out.tokens) },
      (e) => { if (live) setMessage({ tone: "error", text: `Could not load your tokens: ${(e as Error).message}` }) },
    )
    return () => { live = false }
  }, [version])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      const out = await authApi<{ name: string; token: string }>("/tokens", { method: "POST", json: { name } })
      setCreated(out)
      setName("")
      load()
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  async function revoke(token: ApiToken) {
    if (!confirm(`Revoke "${token.name}"? Anything using it stops working immediately.`)) return
    try {
      await authApi(`/tokens/${encodeURIComponent(token.id)}`, { method: "DELETE" })
      setMessage({ tone: "ok", text: `Revoked "${token.name}".` })
      load()
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    }
  }

  const active = tokens.filter((t) => !t.revoked_at)
  return (
    <Section title="API tokens" icon={KeyRound}>
      <p className="text-sm text-muted-foreground">
        For MCP clients and scripts. A token acts as you, with your role and your workspaces. Send it as{" "}
        <code className="font-mono text-xs">Authorization: Bearer …</code>
      </p>
      {created && (
        <CopyOnce label={`"${created.name}" created. Copy it now: it will not be shown again.`} secret={created.token} />
      )}
      <form onSubmit={create} className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor="token-name">Token name</label>
        <input id="token-name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80}
               placeholder="What it is for, e.g. work laptop" className={`${inputClass} min-w-0 flex-1`} />
        <button type="submit" disabled={busy || !name.trim()} className={buttonClass}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          Create token
        </button>
      </form>
      <Notice message={message} />
      {active.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tokens yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {active.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
              <span className="font-medium text-foreground">{t.name}</span>
              <code className="font-mono text-xs text-muted-foreground">{t.prefix}…</code>
              <span className="text-xs text-muted-foreground">
                created {when(t.created_at)} · last used {when(t.last_used_at)}
              </span>
              <button onClick={() => revoke(t)}
                      className="ml-auto flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:border-destructive/50 hover:text-destructive">
                <Trash2 className="h-3.5 w-3.5" /> Revoke
              </button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function ChangePassword() {
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [again, setAgain] = useState("")
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (next !== again) {
      setMessage({ tone: "error", text: "The new passwords do not match." })
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      await authApi("/password", { method: "POST", json: { current_password: current, new_password: next } })
      setCurrent(""); setNext(""); setAgain("")
      // A new password ends every session, this one included (if the old one
      // leaked, so did its sessions). API tokens keep working.
      setMessage({ tone: "ok", text: "Password changed. Every session is signed out; sign in again with the new one." })
      setTimeout(() => window.location.replace("/login"), 2000)
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Password" icon={Lock}>
      <form onSubmit={submit} className="flex max-w-sm flex-col gap-2">
        <label className="flex flex-col gap-1 text-sm text-muted-foreground">
          Current password
          <input type="password" autoComplete="current-password" required value={current}
                 onChange={(e) => setCurrent(e.target.value)} className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm text-muted-foreground">
          New password (at least 10 characters)
          <input type="password" autoComplete="new-password" required minLength={10} value={next}
                 onChange={(e) => setNext(e.target.value)} className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm text-muted-foreground">
          New password, again
          <input type="password" autoComplete="new-password" required minLength={10} value={again}
                 onChange={(e) => setAgain(e.target.value)} className={inputClass} />
        </label>
        <button type="submit" disabled={busy} className={`${buttonClass} self-start`}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Change password
        </button>
        <Notice message={message} />
      </form>
    </Section>
  )
}

export { inputClass, buttonClass, Section }
