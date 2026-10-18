"use client"

/**
 * Sign-in page. When no account exists yet it says how to create the first
 * admin instead of showing a form nobody can use.
 */
import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"

type Status = { auth: string; users: boolean; setup_required: boolean }

/**
 * Where to go after signing in: a path on THIS site, or "/". An open redirect
 * would let a phishing link bounce someone through this login to another site.
 * A prefix check is not enough -- browsers read "/\evil.com" as "//evil.com" --
 * so the target is resolved against our own origin and must stay on it.
 */
function safeNext(next: string | null): string {
  if (!next) return "/"
  try {
    const url = new URL(next, window.location.origin)
    if (url.origin !== window.location.origin) return "/"
    return url.pathname + url.search + url.hash
  } catch {
    return "/"
  }
}

export default function LoginPage() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<Status | null>(null)

  useEffect(() => {
    fetch("/api/auth/status", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await fetch("/session/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setError(body.detail ?? "Sign-in failed.")
        return
      }
      window.location.replace(safeNext(new URLSearchParams(window.location.search).get("next")))
    } catch {
      setError("Could not reach the server.")
    } finally {
      setBusy(false)
    }
  }

  const noAccounts = status && !status.users

  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold">Sign in to Brahmastra</h1>
        <p className="mt-1 text-sm text-muted-foreground">Your team&apos;s shared memory.</p>

        {noAccounts ? (
          <div className="mt-5 space-y-2 text-sm">
            <p>No accounts exist yet. Create the first admin on the server:</p>
            <pre className="overflow-x-auto rounded-md bg-muted p-2 text-xs">
              python -m brahmastra.accounts create-admin --email you@example.com
            </pre>
            <p className="text-muted-foreground">Then reload this page and sign in.</p>
          </div>
        ) : (
          <form onSubmit={submit} className="mt-5 space-y-3">
            <label className="block text-sm">
              <span className="text-muted-foreground">Email</span>
              <input
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </label>
            <label className="block text-sm">
              <span className="text-muted-foreground">Password</span>
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </label>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button type="submit" className="w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        )}
      </Card>
    </main>
  )
}
