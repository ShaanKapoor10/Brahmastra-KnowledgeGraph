"use client"

/**
 * Admins: where the AI tokens go -- the company against its budgets, the last
 * 30 days, each person, each workspace, each kind of work and model. Counts
 * only: the backend sends no note text, question or answer to this screen.
 *
 * Token figures are what the providers billed (llm._record_tokens). Costs are
 * estimates from list prices and are shown only for models with a known price.
 */
import { useEffect, useMemo, useState } from "react"
import { BarChart3, Building2, Download, Gauge, Loader2, Users, Wallet } from "lucide-react"
import { authApi } from "@/lib/auth-api"
import { buttonClass, inputClass, Notice, Section } from "@/components/panels/account-panel"

interface Totals {
  input: number
  output: number
  thoughts: number
  tokens: number
  calls: number
  cost: number
  priced: boolean
}
interface Group extends Totals { key: string }
interface Person extends Totals {
  id: string
  email: string
  name: string | null
  plan: string
  role: string
  disabled: boolean
  questions: number
  notes: number
  words: number
  meeting_hours: number
  tokens_today: number
  tokens_day_limit: number | null
  tokens_month_limit: number | null
}
interface Overview {
  month: string
  today: string
  company: {
    today: Totals
    month: Totals
    budget: { day: number | null; month: number | null }
    cached_replies_month: number
  }
  series: { day: string; input: number; output: number; thoughts: number }[]
  by_purpose: Group[]
  by_model: Group[]
  workspaces: Group[]
  people: Person[]
}
interface PersonDetail {
  daily: ({ day: string } & Totals)[]
  by_purpose: Group[]
}

type Message = { tone: "ok" | "error"; text: string } | null
type SortKey = "tokens" | "tokens_today" | "questions" | "notes" | "cost"

const PURPOSE: Record<string, string> = {
  ask: "Questions", note: "Reading notes", meeting: "Meetings", graph: "Graph building",
  summary: "Topic summaries", resolution: "Entity checks", other: "Other",
}

const n = (v: number) => v.toLocaleString()
const money = (t: Totals) => (t.priced ? `$${t.cost.toFixed(2)}` : t.tokens ? "price unknown" : "$0.00")

export function UsagePanel() {
  const [data, setData] = useState<Overview | null>(null)
  const [message, setMessage] = useState<Message>(null)
  const [version, setVersion] = useState(0)
  const [selected, setSelected] = useState<Person | null>(null)

  useEffect(() => {
    let live = true
    authApi<Overview>("/usage/overview?days=30").then(
      (d) => { if (live) setData(d) },
      (e) => { if (live) setMessage({ tone: "error", text: `Could not load usage: ${(e as Error).message}` }) },
    )
    return () => { live = false }
  }, [version])

  if (!data) {
    return message ? <Notice message={message} /> : (
      <p className="text-sm text-muted-foreground"><Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />Loading usage…</p>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">Usage</h2>
          <p className="text-sm text-muted-foreground">
            AI tokens billed by the provider, for {data.month}. Costs are estimates from list prices.
          </p>
        </div>
        <ExportButton month={data.month} onError={(text) => setMessage({ tone: "error", text })} />
      </header>
      <Notice message={message} />

      <div className="grid gap-3 sm:grid-cols-2">
        <BudgetCard title="Today" used={data.company.today.tokens} budget={data.company.budget.day}
                    note={`${n(data.company.today.calls)} calls · ${money(data.company.today)}`} />
        <BudgetCard title="This month" used={data.company.month.tokens} budget={data.company.budget.month}
                    note={`${n(data.company.month.calls)} calls · ${money(data.company.month)} · ${n(data.company.cached_replies_month)} replies from cache, free`} />
      </div>

      <BudgetEditor budget={data.company.budget} onSaved={() => setVersion((v) => v + 1)} />

      <Section title="Last 30 days" icon={BarChart3}>
        <Trend series={data.series} />
      </Section>

      <Section title={`People (${data.people.length})`} icon={Users}>
        <PeopleTable people={data.people} onPick={setSelected} picked={selected?.id ?? null} />
        {selected && <PersonHistory person={selected} onClose={() => setSelected(null)} />}
      </Section>

      <div className="grid gap-3 lg:grid-cols-3">
        <Section title="By kind of work" icon={Gauge}>
          <GroupList groups={data.by_purpose} label={(k) => PURPOSE[k] ?? k} />
        </Section>
        <Section title="Workspaces (shared work)" icon={Building2}>
          <GroupList groups={data.workspaces} label={(k) => k.replace(/^ws:/, "")}
                     empty="No shared work this month." />
        </Section>
        <Section title="By model" icon={Wallet}>
          <GroupList groups={data.by_model.filter((g) => g.key !== "cache")} label={(k) => k} />
        </Section>
      </div>
    </div>
  )
}

function BudgetCard({ title, used, budget, note }: { title: string; used: number; budget: number | null; note: string }) {
  const share = budget ? Math.min(1, used / budget) : 0
  const tone = !budget ? "bg-primary" : share >= 1 ? "bg-destructive" : share >= 0.8 ? "bg-amber-400" : "bg-primary"
  return (
    <section className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{title}</h3>
        <span className="font-mono text-xs text-muted-foreground">{budget ? `budget ${n(budget)}` : "no budget set"}</span>
      </div>
      <p className="font-mono text-2xl font-semibold tabular-nums text-foreground">{n(used)} <span className="text-sm font-normal text-muted-foreground">tokens</span></p>
      {budget ? (
        <div className="h-1.5 overflow-hidden rounded-full bg-secondary" role="progressbar" aria-label={`${title} against budget`}
             aria-valuemin={0} aria-valuemax={budget} aria-valuenow={used}>
          <div className={`h-full ${tone}`} style={{ width: `${Math.round(share * 100)}%` }} />
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">{note}</p>
    </section>
  )
}

function BudgetEditor({ budget, onSaved }: { budget: Overview["company"]["budget"]; onSaved: () => void }) {
  const [day, setDay] = useState(budget.day?.toString() ?? "")
  const [month, setMonth] = useState(budget.month?.toString() ?? "")
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<Message>(null)
  const parse = (v: string) => (v.trim() === "" ? null : Math.max(0, Math.floor(Number(v))))

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      const out = await authApi<{ day: number | null; month: number | null }>("/usage/budget", {
        method: "PUT", json: { tokens_day: parse(day), tokens_month: parse(month) },
      })
      setDay(out.day?.toString() ?? "")
      setMonth(out.month?.toString() ?? "")
      setMessage({ tone: "ok", text: "Company budget saved. A server setting may cap it lower." })
      onSaved()
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Company token budget" icon={Wallet}>
      <form onSubmit={save} className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Per day
          <input inputMode="numeric" value={day} onChange={(e) => setDay(e.target.value)} placeholder="no budget"
                 className={`${inputClass} w-40 font-mono`} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Per month
          <input inputMode="numeric" value={month} onChange={(e) => setMonth(e.target.value)} placeholder="no budget"
                 className={`${inputClass} w-40 font-mono`} />
        </label>
        <button type="submit" disabled={busy} className={buttonClass}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Save
        </button>
      </form>
      <p className="text-xs text-muted-foreground">
        When a budget is used up, AI work pauses for everyone until it resets; search and browsing keep working and
        nothing is lost. Empty means no company budget.
      </p>
      <Notice message={message} />
    </Section>
  )
}

function Trend({ series }: { series: Overview["series"] }) {
  const max = Math.max(1, ...series.map((d) => d.input + d.output + d.thoughts))
  const total = series.reduce((s, d) => s + d.input + d.output + d.thoughts, 0)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex h-36 items-end gap-[3px]" role="img"
           aria-label={`Tokens per day for the last ${series.length} days, ${n(total)} in total`}>
        {series.map((d) => {
          const sum = d.input + d.output + d.thoughts
          return (
            <div key={d.day} className="flex h-full min-w-0 flex-1 flex-col justify-end"
                 title={`${d.day}: ${n(sum)} tokens (${n(d.input)} in, ${n(d.output)} out, ${n(d.thoughts)} thinking)`}>
              <div className="bg-amber-400/80" style={{ height: `${(d.thoughts / max) * 100}%` }} />
              <div className="bg-sky-400/80" style={{ height: `${(d.output / max) * 100}%` }} />
              <div className="rounded-t-sm bg-primary/80" style={{ height: `${(d.input / max) * 100}%` }} />
            </div>
          )
        })}
      </div>
      <div className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
        <span>{series[0]?.day}</span>
        <span className="flex gap-3">
          <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-primary/80" />input</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-sky-400/80" />output</span>
          <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-amber-400/80" />thinking</span>
        </span>
        <span>{series[series.length - 1]?.day}</span>
      </div>
    </div>
  )
}

function PeopleTable({ people, onPick, picked }: { people: Person[]; onPick: (p: Person) => void; picked: string | null }) {
  const [sort, setSort] = useState<SortKey>("tokens")
  const rows = useMemo(() => [...people].sort((a, b) => (b[sort] as number) - (a[sort] as number)), [people, sort])
  const head = (key: SortKey, label: string) => (
    <th className="py-1 pr-3 text-right font-normal">
      <button onClick={() => setSort(key)} aria-pressed={sort === key}
              className={sort === key ? "text-foreground" : "hover:text-foreground"}>{label}{sort === key ? " ↓" : ""}</button>
    </th>
  )
  const percent = (used: number, limit: number | null) => (limit ? `${Math.round((used / limit) * 100)}%` : "—")
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 font-normal">Person</th>
            <th className="py-1 pr-3 font-normal">Plan</th>
            {head("questions", "Questions")}
            {head("notes", "Notes")}
            {head("tokens_today", "Today")}
            {head("tokens", "This month")}
            <th className="py-1 pr-3 text-right font-normal">Of budget</th>
            {head("cost", "Est. cost")}
          </tr>
        </thead>
        <tbody className="divide-y divide-border font-mono tabular-nums text-foreground">
          {rows.map((p) => (
            <tr key={p.id} onClick={() => onPick(p)} aria-selected={picked === p.id}
                className={`cursor-pointer hover:bg-secondary/60 ${picked === p.id ? "bg-secondary" : ""} ${p.disabled ? "opacity-60" : ""}`}>
              <td className="py-1.5 pr-3 font-sans">{p.name || p.email}</td>
              <td className="py-1.5 pr-3 font-sans">{p.plan}</td>
              <td className="py-1.5 pr-3 text-right">{n(p.questions)}</td>
              <td className="py-1.5 pr-3 text-right">{n(p.notes)}</td>
              <td className="py-1.5 pr-3 text-right">{n(p.tokens_today)}</td>
              <td className="py-1.5 pr-3 text-right">{n(p.tokens)}</td>
              <td className="py-1.5 pr-3 text-right">{percent(p.tokens, p.tokens_month_limit)}</td>
              <td className="py-1.5 pr-3 text-right">{money(p)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted-foreground">Select a person to see their last 30 days.</p>
    </div>
  )
}

function PersonHistory({ person, onClose }: { person: Person; onClose: () => void }) {
  const [detail, setDetail] = useState<PersonDetail | null>(null)
  useEffect(() => {
    let live = true
    authApi<PersonDetail>(`/usage/users/${person.id}?days=30`).then((d) => { if (live) setDetail(d) }, () => {})
    return () => { live = false }
  }, [person.id])
  return (
    <div className="mt-3 flex flex-col gap-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-foreground">{person.name || person.email}, last 30 days</span>
        <button onClick={onClose} className="text-xs text-muted-foreground hover:text-foreground">Close</button>
      </div>
      {!detail ? (
        <p className="text-xs text-muted-foreground"><Loader2 className="mr-1 inline h-3 w-3 animate-spin" />Loading…</p>
      ) : detail.daily.length === 0 ? (
        <p className="text-xs text-muted-foreground">No AI use in the last 30 days.</p>
      ) : (
        <>
          <GroupList groups={detail.by_purpose} label={(k) => PURPOSE[k] ?? k} />
          <ul className="grid gap-x-4 font-mono text-xs tabular-nums text-muted-foreground sm:grid-cols-2">
            {detail.daily.map((d) => (
              <li key={d.day} className="flex justify-between"><span>{d.day}</span><span className="text-foreground">{n(d.tokens)}</span></li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function GroupList({ groups, label, empty = "Nothing yet this month." }: {
  groups: Group[]; label: (key: string) => string; empty?: string
}) {
  if (groups.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>
  const max = Math.max(1, ...groups.map((g) => g.tokens))
  return (
    <ul className="flex flex-col gap-2">
      {groups.map((g) => (
        <li key={g.key} className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate text-foreground">{label(g.key)}</span>
            <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{n(g.tokens)} · {money(g)}</span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-secondary">
            <div className="h-full bg-primary/70" style={{ width: `${(g.tokens / max) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  )
}

function ExportButton({ month, onError }: { month: string; onError: (text: string) => void }) {
  const [busy, setBusy] = useState(false)
  async function download() {
    setBusy(true)
    try {
      const res = await fetch("/api/auth/usage/export.csv", { cache: "no-store" })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      const url = URL.createObjectURL(await res.blob())
      const a = Object.assign(document.createElement("a"), { href: url, download: `brahmastra-usage-${month}.csv` })
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      onError(`Export failed: ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }
  return (
    <button onClick={download} disabled={busy} className={buttonClass}>
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />} Export CSV
    </button>
  )
}
