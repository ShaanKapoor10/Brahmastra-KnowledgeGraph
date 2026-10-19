/**
 * Calls to the account routes (/auth/*), through the dashboard's /api proxy,
 * which attaches the signed-in person's own session. The backend decides what
 * each caller may do; these screens only hide what would be refused anyway.
 */

export interface AccountUser {
  id: string
  email: string
  name: string | null
  role: "viewer" | "member" | "admin"
  disabled: boolean
  created_at: number
  last_login_at: number | null
  workspaces: string[]
  /** Plan id (lib/plans.ts). */
  plan: string
}

export interface ApiToken {
  id: string
  name: string
  prefix: string
  created_at: number
  last_used_at: number | null
  revoked_at: number | null
}

export interface AuditEntry {
  at: number
  actor: string | null
  action: string
  target: string | null
  detail: string | null
  ip: string | null
}

export async function authApi<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {}
  const res = await fetch(`/api/auth${path}`, {
    cache: "no-store",
    ...rest,
    ...(json !== undefined
      ? { body: JSON.stringify(json), headers: { "Content-Type": "application/json", ...rest.headers } }
      : {}),
  })
  const text = await res.text()
  const body = text ? JSON.parse(text) : null
  if (!res.ok) {
    const detail = body?.detail
    // FastAPI validation errors arrive as a list; show the first message.
    const message = Array.isArray(detail) ? detail[0]?.msg : typeof detail === "string" ? detail : null
    throw new Error(message ?? `${res.status} ${res.statusText}`)
  }
  return body as T
}

/** Seconds since the epoch, as the backend stores them, for people to read. */
export function when(seconds: number | null | undefined): string {
  if (!seconds) return "never"
  return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

/**
 * A temporary password for a new account: 20 characters from an alphabet with
 * no look-alikes (0/O, 1/l/I), from the browser's cryptographic generator.
 * Shown once to the admin, who passes it on; the person changes it on sign-in.
 */
export function temporaryPassword(): string {
  const alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  // Bytes at or above the largest multiple of the alphabet's length are
  // rejected, so every symbol is equally likely (a plain modulo is not).
  const limit = 256 - (256 % alphabet.length)
  let out = ""
  while (out.length < 20) {
    const bytes = new Uint8Array(32)
    crypto.getRandomValues(bytes)
    for (const b of bytes) if (b < limit && out.length < 20) out += alphabet[b % alphabet.length]
  }
  return out
}
