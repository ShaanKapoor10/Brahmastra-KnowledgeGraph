"use client"

/**
 * Admins: the people who can sign in, what each may do, which workspaces each
 * sees, and the audit log. Everything here is also enforced by the backend;
 * the screen only saves typing the API calls.
 *
 * There is no invitation email (an install need not run a mail server): an
 * admin creates the account with a temporary password, shown once, and the
 * person changes it under Account.
 */
import { useEffect, useState } from "react"
import { History, Loader2, Plus, RotateCw, ShieldCheck, Trash2, UserPlus, Users } from "lucide-react"
import { authApi, temporaryPassword, when, type AccountUser, type AuditEntry } from "@/lib/auth-api"
import type { SignedInUser } from "@/components/dashboard"
import type { WorkspaceOption } from "@/components/workspace-switcher"
import { buttonClass, CopyOnce, inputClass, Notice, Section } from "@/components/panels/account-panel"
import type { Plan, PlanCatalog } from "@/lib/plans"

const ROLES: AccountUser["role"][] = ["viewer", "member", "admin"]
const ROLE_HELP: Record<AccountUser["role"], string> = {
  viewer: "read, search and ask",
  member: "also add notes, ingest sessions and run the pipeline",
  admin: "everything, in every workspace, including this screen",
}

type Message = { tone: "ok" | "error"; text: string } | null

/** The plan each role starts on (brahmastra/plans.py ROLE_PLAN). */
const ROLE_PLAN_NAME: Record<AccountUser["role"], string> = {
  viewer: "Reader", member: "Contributor", admin: "Unlimited",
}

export function TeamPanel({ user, workspaces }: { user: SignedInUser; workspaces: WorkspaceOption[] }) {
  const [people, setPeople] = useState<AccountUser[]>([])
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState<Message>(null)
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null)
  const [plans, setPlans] = useState<Plan[]>([])
  useEffect(() => {
    let live = true
    authApi<PlanCatalog>("/plans").then((c) => { if (live) setPlans(c.plans) }, () => {})
    return () => { live = false }
  }, [])

  // Bumped after every change; the effect below reloads on it.
  const [version, setVersion] = useState(0)
  const load = () => setVersion((v) => v + 1)
  useEffect(() => {
    let live = true            // a reply for an unmounted panel is dropped
    authApi<{ users: AccountUser[] }>("/users").then(
      (out) => { if (live) { setPeople(out.users); setLoading(false) } },
      (e) => {
        if (!live) return
        setMessage({ tone: "error", text: `Could not load people: ${(e as Error).message}` })
        setLoading(false)
      },
    )
    return () => { live = false }
  }, [version])

  async function act(run: () => Promise<unknown>, done: string) {
    setMessage(null)
    try {
      await run()
      setMessage({ tone: "ok", text: done })
      load()
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message })
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h2 className="text-base font-semibold text-foreground">Team</h2>
        <p className="text-sm text-muted-foreground">
          Who can sign in, what each person may do, and which workspaces each one sees. Admins see every workspace.
        </p>
      </header>

      <AddPerson workspaces={workspaces} plans={plans} onCreated={(email, password) => {
        setSecret({ label: `Account created for ${email}. Give them this temporary password; it is shown only once.`, value: password })
        load()
      }} />

      {secret && <CopyOnce label={secret.label} secret={secret.value} />}
      <Notice message={message} />

      <Section title={`People (${people.length})`} icon={Users}>
        {loading ? (
          <p className="text-sm text-muted-foreground"><Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />Loading…</p>
        ) : (
          <ul className="divide-y divide-border">
            {people.map((p) => (
              <Person key={p.id} person={p} me={user} workspaces={workspaces} plans={plans}
                      onChange={(patch, done) => act(() => authApi(`/users/${p.id}`, { method: "PATCH", json: patch }), done)}
                      onPlan={(planId) => act(() => authApi(`/users/${p.id}/plan`, { method: "PUT", json: { plan_id: planId } }),
                                              `${p.email} is now on ${plans.find((x) => x.id === planId)?.name ?? planId}.`)}
                      onReset={() => {
                        const password = temporaryPassword()
                        void act(async () => {
                          await authApi(`/users/${p.id}/password`, { method: "POST", json: { password } })
                          setSecret({ label: `New temporary password for ${p.email}. Their sessions were signed out.`, value: password })
                        }, `Password reset for ${p.email}.`)
                      }}
                      onDelete={() => {
                        if (!confirm(`Remove ${p.email}? They lose access immediately. Their notes stay.`)) return
                        void act(() => authApi(`/users/${p.id}`, { method: "DELETE" }), `Removed ${p.email}.`)
                      }} />
            ))}
          </ul>
        )}
      </Section>

      <AuditLog />
    </div>
  )
}

function WorkspacePicker({ all, chosen, onChange, disabled }: {
  all: WorkspaceOption[]; chosen: string[]; onChange: (next: string[]) => void; disabled?: boolean
}) {
  if (all.length === 0) return <span className="text-xs text-muted-foreground">no workspaces</span>
  return (
    <div className="flex flex-wrap gap-1.5">
      {all.map((w) => {
        const on = chosen.includes(w.id)
        return (
          <button key={w.id} type="button" disabled={disabled} aria-pressed={on}
                  onClick={() => onChange(on ? chosen.filter((x) => x !== w.id) : [...chosen, w.id])}
                  className={`rounded-full border px-2 py-0.5 font-mono text-[11px] transition-colors disabled:opacity-50 ${
                    on ? "border-primary/50 bg-primary/15 text-primary" : "border-border text-muted-foreground hover:text-foreground"
                  }`}>
            {w.name || w.id}
          </button>
        )
      })}
    </div>
  )
}

function AddPerson({ workspaces, plans, onCreated }: {
  workspaces: WorkspaceOption[]; plans: Plan[]; onCreated: (email: string, password: string) => void
}) {
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [role, setRole] = useState<AccountUser["role"]>("member")
  const [chosen, setChosen] = useState<string[]>([])
  const [plan, setPlan] = useState("")            // "": the role's own plan
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<Message>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMessage(null)
    const password = temporaryPassword()
    try {
      await authApi("/users", { method: "POST", json: { email, name: name || null, role, password, workspaces: chosen,
                                                       plan: plan || null } })
      onCreated(email, password)
      setEmail(""); setName(""); setChosen([]); setRole("member"); setPlan("")
    } catch (err) {
      setMessage({ tone: "error", text: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Add a person" icon={UserPlus}>
      <form onSubmit={submit} className="flex flex-col gap-2.5">
        <div className="flex flex-wrap gap-2">
          <label className="sr-only" htmlFor="new-email">Email</label>
          <input id="new-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                 placeholder="email@yourcompany.com" className={`${inputClass} min-w-0 flex-1`} />
          <label className="sr-only" htmlFor="new-name">Name</label>
          <input id="new-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120}
                 placeholder="Name (optional)" className={`${inputClass} min-w-0 flex-1`} />
          <label className="sr-only" htmlFor="new-role">Role</label>
          <select id="new-role" value={role} onChange={(e) => setRole(e.target.value as AccountUser["role"])}
                  className={inputClass}>
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <label className="sr-only" htmlFor="new-plan">Plan</label>
          <select id="new-plan" value={plan} onChange={(e) => setPlan(e.target.value)} className={inputClass}>
            <option value="">plan: {ROLE_PLAN_NAME[role]} (the role&apos;s)</option>
            {plans.map((p) => <option key={p.id} value={p.id}>plan: {p.name}</option>)}
          </select>
        </div>
        <p className="text-xs text-muted-foreground">{role}: {ROLE_HELP[role]}. The plan decides which features and how much.</p>
        {role !== "admin" && (
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">Workspaces they can open</span>
            <WorkspacePicker all={workspaces} chosen={chosen} onChange={setChosen} />
          </div>
        )}
        <button type="submit" disabled={busy || !email} className={`${buttonClass} self-start`}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          Create account
        </button>
        <Notice message={message} />
      </form>
    </Section>
  )
}

function Person({ person, me, workspaces, plans, onChange, onPlan, onReset, onDelete }: {
  person: AccountUser
  me: SignedInUser
  workspaces: WorkspaceOption[]
  plans: Plan[]
  onPlan: (planId: string) => void
  onChange: (patch: Partial<Pick<AccountUser, "role" | "disabled" | "workspaces">>, done: string) => void
  onReset: () => void
  onDelete: () => void
}) {
  const isMe = me.email === person.email
  return (
    <li className={`flex flex-col gap-2 py-3 ${person.disabled ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-foreground">{person.name || person.email}</span>
        {person.name && <span className="text-sm text-muted-foreground">{person.email}</span>}
        {isMe && <span className="rounded-full border border-border px-1.5 text-[10px] text-muted-foreground">you</span>}
        {person.disabled && (
          <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-1.5 text-[10px] text-amber-300">disabled</span>
        )}
        <span className="ml-auto text-xs text-muted-foreground">last sign-in {when(person.last_login_at)}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`role-${person.id}`}>Role for {person.email}</label>
        <select id={`role-${person.id}`} value={person.role} disabled={isMe}
                title={isMe ? "You cannot change your own role" : ROLE_HELP[person.role]}
                onChange={(e) => onChange({ role: e.target.value as AccountUser["role"] }, `${person.email} is now ${e.target.value}.`)}
                className={`${inputClass} py-1 text-xs`}>
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <label className="sr-only" htmlFor={`plan-${person.id}`}>Plan for {person.email}</label>
        <select id={`plan-${person.id}`} value={person.plan} onChange={(e) => onPlan(e.target.value)}
                title={isMe ? "Another admin must change your plan, unless you are the only admin" : "Which features, and how much"}
                className={`${inputClass} py-1 text-xs`}>
          {plans.length === 0 && <option value={person.plan}>{person.plan}</option>}
          {plans.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        {person.role === "admin" ? (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5" /> every workspace
          </span>
        ) : (
          <WorkspacePicker all={workspaces} chosen={person.workspaces}
                           onChange={(next) => onChange({ workspaces: next }, `Workspaces updated for ${person.email}.`)} />
        )}
        {!isMe && (
          <div className="ml-auto flex gap-1.5">
            <button onClick={() => onChange({ disabled: !person.disabled }, `${person.email} ${person.disabled ? "enabled" : "disabled"}.`)}
                    className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground">
              {person.disabled ? "Enable" : "Disable"}
            </button>
            <button onClick={onReset}
                    className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground">
              <RotateCw className="h-3 w-3" /> Reset password
            </button>
            <button onClick={onDelete}
                    className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:border-destructive/50 hover:text-destructive">
              <Trash2 className="h-3 w-3" /> Remove
            </button>
          </div>
        )}
      </div>
    </li>
  )
}

function AuditLog() {
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [message, setMessage] = useState<Message>(null)
  useEffect(() => {
    let live = true
    authApi<{ entries: AuditEntry[] }>("/audit?limit=100").then(
      (out) => { if (live) setEntries(out.entries) },
      (e) => { if (live) setMessage({ tone: "error", text: `Could not load the audit log: ${(e as Error).message}` }) },
    )
    return () => { live = false }
  }, [])

  return (
    <Section title="Audit log (latest 100)" icon={History}>
      <Notice message={message} />
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-muted-foreground">
            <tr><th className="py-1 pr-3 font-normal">When</th><th className="py-1 pr-3 font-normal">Who</th>
              <th className="py-1 pr-3 font-normal">What</th><th className="py-1 pr-3 font-normal">Subject</th>
              <th className="py-1 font-normal">From</th></tr>
          </thead>
          <tbody className="divide-y divide-border font-mono text-foreground">
            {entries.map((e, i) => (
              <tr key={`${e.at}-${i}`}>
                <td className="whitespace-nowrap py-1 pr-3 tabular-nums">{when(e.at)}</td>
                <td className="py-1 pr-3">{e.actor ?? "—"}</td>
                <td className="py-1 pr-3">{e.action}</td>
                <td className="py-1 pr-3">{e.target ?? ""}{e.detail ? ` (${e.detail})` : ""}</td>
                <td className="py-1">{e.ip ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}
