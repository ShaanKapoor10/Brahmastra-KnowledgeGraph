"use client"

/**
 * Upgrades: a person asks for another plan or a one-off top-up (Account), and
 * an admin approves or declines it with the person's usage in view (Team).
 * Admins also edit what each plan includes. The backend enforces every rule
 * here -- nobody decides their own request while another admin exists, a
 * top-up lasts one day or month, an end date returns the person to their
 * previous plan -- and records each step in the audit log.
 */
import { useEffect, useState } from "react"
import { Check, Inbox, Loader2, Send, SlidersHorizontal, X } from "lucide-react"
import { authApi, when } from "@/lib/auth-api"
import type { Plan, PlanCatalog, UsageRow } from "@/lib/plans"
import { buttonClass, inputClass, Notice, Section } from "@/components/panels/account-panel"

export interface PlanRequest {
  id: string
  user_id: string
  email: string | null
  name: string | null
  kind: "plan" | "topup"
  plan_id: string | null
  limit_key: string | null
  amount: number | null
  reason: string
  status: "pending" | "approved" | "declined" | "cancelled"
  created_at: number
  decided_by: string | null
  decided_at: number | null
  decision_note: string | null
  expires_at: number | null
  usage?: UsageRow[]
  current_plan?: string | null
}

type Message = { tone: "ok" | "error"; text: string } | null

const STATUS_TONE: Record<PlanRequest["status"], string> = {
  pending: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  approved: "border-green-500/40 bg-green-500/10 text-green-400",
  declined: "border-destructive/40 bg-destructive/10 text-destructive",
  cancelled: "border-border text-muted-foreground",
}

function describe(r: PlanRequest, catalog: PlanCatalog | null): string {
  if (r.kind === "plan") return `Move to ${catalog?.plans.find((p) => p.id === r.plan_id)?.name ?? r.plan_id}`
  const limit = catalog?.limits[r.limit_key ?? ""] ?? r.limit_key
  const scope = r.limit_key?.endsWith("_day") ? "today" : "this month"
  return `+${(r.amount ?? 0).toLocaleString()} ${limit?.toLowerCase()} (${scope})`
}

function useCatalog() {
  const [catalog, setCatalog] = useState<PlanCatalog | null>(null)
  useEffect(() => {
    let live = true
    authApi<PlanCatalog>("/plans").then((c) => { if (live) setCatalog(c) }, () => {})
    return () => { live = false }
  }, [])
  return catalog
}

// ---------------------------------------------------------------- Account: ask for more

export function MyRequests({ planId }: { planId: string | null }) {
  const catalog = useCatalog()
  const [requests, setRequests] = useState<PlanRequest[]>([])
  const [version, setVersion] = useState(0)
  const [kind, setKind] = useState<"plan" | "topup">("plan")
  const [plan, setPlan] = useState("")
  const [limitKey, setLimitKey] = useState("questions_month")
  const [amount, setAmount] = useState("100")
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<Message>(null)

  useEffect(() => {
    let live = true
    authApi<{ requests: PlanRequest[] }>("/me/requests").then((r) => { if (live) setRequests(r.requests) }, () => {})
    return () => { live = false }
  }, [version])

  const otherPlans = (catalog?.plans ?? []).filter((p) => p.id !== planId)
  const chosenPlan = plan || otherPlans[0]?.id || ""

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      await authApi("/me/requests", {
        method: "POST",
        json: kind === "plan"
          ? { kind, plan_id: chosenPlan, reason }
          : { kind, limit_key: limitKey, amount: Math.floor(Number(amount)), reason },
      })
      setReason("")
      setMessage({ tone: "ok", text: "Sent. An admin will see it on the Team screen." })
      setVersion((v) => v + 1)
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  async function cancel(id: string) {
    setMessage(null)
    try {
      await authApi(`/me/requests/${id}`, { method: "DELETE" })
      setVersion((v) => v + 1)
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    }
  }

  return (
    <Section title="Ask for more" icon={Send}>
      <form onSubmit={submit} className="flex flex-col gap-2.5">
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="What to ask for">
          {(["plan", "topup"] as const).map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)}
                    className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
                      kind === k ? "border-primary/50 bg-primary/15 text-primary" : "border-border text-muted-foreground hover:text-foreground"
                    }`}>
              {k === "plan" ? "A different plan" : "A one-off top-up"}
            </button>
          ))}
        </div>
        {kind === "plan" ? (
          <select aria-label="Plan" value={chosenPlan} onChange={(e) => setPlan(e.target.value)} className={inputClass}>
            {otherPlans.map((p) => <option key={p.id} value={p.id}>{p.name}{p.description ? `: ${p.description}` : ""}</option>)}
          </select>
        ) : (
          <div className="flex flex-wrap gap-2">
            <select aria-label="Which limit" value={limitKey} onChange={(e) => setLimitKey(e.target.value)} className={inputClass}>
              {Object.entries(catalog?.limits ?? {}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            <input aria-label="How much more" inputMode="numeric" value={amount} onChange={(e) => setAmount(e.target.value)}
                   className={`${inputClass} w-32 font-mono`} />
          </div>
        )}
        <textarea aria-label="Why" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} rows={2}
                  placeholder="Why you need it, in a sentence" className={`${inputClass} resize-none`} />
        <button type="submit" disabled={busy || reason.trim().length < 3 || (kind === "plan" && !chosenPlan)}
                className={`${buttonClass} self-start`}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />} Send request
        </button>
        <Notice message={message} />
      </form>
      {requests.length > 0 && (
        <ul className="divide-y divide-border">
          {requests.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
              <span className={`rounded-full border px-1.5 text-[10px] ${STATUS_TONE[r.status]}`}>{r.status}</span>
              <span className="text-foreground">{describe(r, catalog)}</span>
              <span className="text-xs text-muted-foreground">{when(r.created_at)}</span>
              {r.decision_note && <span className="w-full text-xs text-muted-foreground">“{r.decision_note}”</span>}
              {r.status === "approved" && r.expires_at && (
                <span className="w-full text-xs text-muted-foreground">Until {when(r.expires_at)}.</span>
              )}
              {r.status === "pending" && (
                <button onClick={() => cancel(r.id)} className="ml-auto text-xs text-muted-foreground hover:text-foreground">
                  Cancel
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------- Team: the queue

export function RequestQueue({ onDecided }: { onDecided: () => void }) {
  const catalog = useCatalog()
  const [requests, setRequests] = useState<PlanRequest[] | null>(null)
  const [version, setVersion] = useState(0)
  const [message, setMessage] = useState<Message>(null)

  useEffect(() => {
    let live = true
    authApi<{ requests: PlanRequest[] }>("/requests?status=pending").then(
      (r) => { if (live) setRequests(r.requests) },
      (e) => { if (live) setMessage({ tone: "error", text: (e as Error).message }) },
    )
    return () => { live = false }
  }, [version])

  async function decide(r: PlanRequest, approve: boolean, note: string, until: string) {
    setMessage(null)
    try {
      const expires_at = approve && until ? new Date(`${until}T23:59:59`).getTime() / 1000 : null
      await authApi(`/requests/${r.id}/${approve ? "approve" : "decline"}`, {
        method: "POST", json: { note: note || null, expires_at },
      })
      setMessage({ tone: "ok", text: `${approve ? "Approved" : "Declined"}: ${r.email}, ${describe(r, catalog).toLowerCase()}.` })
      setVersion((v) => v + 1)
      onDecided()
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    }
  }

  return (
    <Section title={`Requests waiting (${requests?.length ?? 0})`} icon={Inbox}>
      <Notice message={message} />
      {requests === null ? (
        <p className="text-sm text-muted-foreground"><Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />Loading…</p>
      ) : requests.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing waiting.</p>
      ) : (
        <ul className="divide-y divide-border">
          {requests.map((r) => <QueueItem key={r.id} request={r} catalog={catalog} onDecide={decide} />)}
        </ul>
      )}
    </Section>
  )
}

function QueueItem({ request: r, catalog, onDecide }: {
  request: PlanRequest; catalog: PlanCatalog | null
  onDecide: (r: PlanRequest, approve: boolean, note: string, until: string) => void
}) {
  const [note, setNote] = useState("")
  const [until, setUntil] = useState("")
  const current = catalog?.plans.find((p) => p.id === r.current_plan)?.name ?? r.current_plan
  return (
    <li className="flex flex-col gap-2 py-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-medium text-foreground">{r.name || r.email}</span>
        <span className="text-sm text-muted-foreground">on {current}</span>
        <span className="text-sm text-foreground">asks: {describe(r, catalog)}</span>
        <span className="ml-auto text-xs text-muted-foreground">{when(r.created_at)}</span>
      </div>
      <p className="text-sm text-muted-foreground">“{r.reason}”</p>
      {r.usage && r.usage.length > 0 && (
        <p className="font-mono text-[11px] text-muted-foreground">
          This month:{" "}
          {r.usage.filter((u) => u.limit !== 0).map((u) => (
            `${catalog?.limits[u.limit_key]?.replace(" per month", "") ?? u.meter} ${u.used.toLocaleString()}${u.limit != null ? `/${u.limit.toLocaleString()}` : ""}`
          )).join(" · ")}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input aria-label="Note to the person" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500}
               placeholder="Note (optional)" className={`${inputClass} min-w-0 flex-1 py-1 text-xs`} />
        {r.kind === "plan" && (
          <label className="flex items-center gap-1 text-xs text-muted-foreground">
            until
            <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} className={`${inputClass} py-1 text-xs`} />
          </label>
        )}
        <button onClick={() => onDecide(r, true, note, until)} className={`${buttonClass} py-1 text-xs`}>
          <Check className="h-3 w-3" /> Approve
        </button>
        <button onClick={() => onDecide(r, false, note, until)}
                className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:border-destructive/50 hover:text-destructive">
          <X className="h-3 w-3" /> Decline
        </button>
      </div>
      {r.kind === "plan" && <p className="text-xs text-muted-foreground">Leave the date empty for a permanent change; with a date, they return to {current} after it.</p>}
    </li>
  )
}

// ---------------------------------------------------------------- Team: editing plans

export function PlanEditor() {
  const [catalog, setCatalog] = useState<PlanCatalog | null>(null)
  const [version, setVersion] = useState(0)
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    authApi<PlanCatalog>("/plans").then((c) => { if (live) setCatalog(c) }, () => {})
    return () => { live = false }
  }, [version])
  if (!catalog) return null
  return (
    <Section title="Plans" icon={SlidersHorizontal}>
      <p className="text-sm text-muted-foreground">
        A change applies to everyone on the plan at their next request, and is recorded in the audit log. Empty means no limit.
      </p>
      <ul className="divide-y divide-border">
        {catalog.plans.map((p) => (
          <li key={p.id} className="py-2">
            <button onClick={() => setOpen(open === p.id ? null : p.id)} aria-expanded={open === p.id}
                    className="flex w-full items-baseline justify-between gap-3 text-left">
              <span className="font-medium text-foreground">{p.name}</span>
              <span className="text-xs text-muted-foreground">{p.features.length} features · {open === p.id ? "close" : "edit"}</span>
            </button>
            {open === p.id && <EditOne plan={p} catalog={catalog} onSaved={() => setVersion((v) => v + 1)} />}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function EditOne({ plan, catalog, onSaved }: { plan: Plan; catalog: PlanCatalog; onSaved: () => void }) {
  const [features, setFeatures] = useState<string[]>(plan.features)
  const [limits, setLimits] = useState<Record<string, string>>(
    Object.fromEntries(Object.keys(catalog.limits).map((k) => [k, plan.limits[k] == null ? "" : String(plan.limits[k])])),
  )
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<Message>(null)
  const locked = plan.id === "unlimited"

  async function save() {
    setBusy(true)
    setMessage(null)
    try {
      const parsed = Object.fromEntries(Object.entries(limits).map(([k, v]) => [k, v.trim() === "" ? null : Math.floor(Number(v))]))
      await authApi(`/plans/${plan.id}`, { method: "PUT", json: { features: locked ? undefined : features, limits: parsed } })
      setMessage({ tone: "ok", text: `${plan.name} saved.` })
      onSaved()
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 flex flex-col gap-3 rounded-md border border-border p-3">
      <fieldset className="flex flex-wrap gap-1.5" disabled={locked}>
        <legend className="mb-1 text-xs text-muted-foreground">{locked ? "Features (fixed for the admins' plan)" : "Features"}</legend>
        {Object.entries(catalog.features).map(([id, name]) => {
          const on = features.includes(id)
          return (
            <button key={id} type="button" aria-pressed={on}
                    onClick={() => setFeatures(on ? features.filter((f) => f !== id) : [...features, id])}
                    className={`rounded-full border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-60 ${
                      on ? "border-primary/50 bg-primary/15 text-primary" : "border-border text-muted-foreground hover:text-foreground"
                    }`}>
              {name}
            </button>
          )
        })}
      </fieldset>
      <div className="grid gap-2 sm:grid-cols-2">
        {Object.entries(catalog.limits).map(([key, label]) => (
          <label key={key} className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            {label}
            <input inputMode="numeric" value={limits[key]} placeholder="no limit"
                   onChange={(e) => setLimits({ ...limits, [key]: e.target.value })}
                   className={`${inputClass} w-32 py-1 text-right font-mono text-xs`} />
          </label>
        ))}
      </div>
      <div className="flex items-center gap-3">
        <button onClick={save} disabled={busy} className={buttonClass}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Save {plan.name}
        </button>
        <Notice message={message} />
      </div>
    </div>
  )
}
