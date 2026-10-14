/**
 * Server-side access to the Brahmastra API, as the signed-in user.
 *
 * Every call goes through the server: React Server Components and server
 * actions use backendFetch() directly, and browser code goes through the
 * /api/[...path] proxy route, which calls this.
 *
 * WHOSE CREDENTIAL. The dashboard used to attach BRAHMASTRA_API_KEY -- the
 * shared admin key -- to every request, so anyone who could open the dashboard
 * held the whole API: read every workspace, write notes, run the pipeline,
 * write into Notion. Now the credential is the user's own session, kept in an
 * httpOnly cookie that browser JavaScript cannot read, and the backend decides
 * what that user may do.
 *
 * DASHBOARD_SERVICE_KEY=1 restores the old behaviour for a dashboard that only
 * trusted people can reach (a laptop, a private network) and that has no
 * accounts yet. It is an explicit opt-in because it hands every visitor admin.
 */
import { cookies, headers as requestHeaders } from "next/headers"

export const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8001"
export const SESSION_COOKIE = "brahmastra_session"

/** The session token from the request's cookie, if any. Server only. */
export async function sessionToken(): Promise<string | undefined> {
  try {
    return (await cookies()).get(SESSION_COOKIE)?.value || undefined
  } catch {
    // Outside a request (build time) there are no cookies.
    return undefined
  }
}

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/

function hex(bytes: number): string {
  const a = new Uint8Array(bytes)
  crypto.getRandomValues(a)
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("")
}

/**
 * W3C Trace Context for the backend call: continue the incoming trace (a proxy
 * in front may have started one), else start one here. The backend logs its
 * trace id on every line and returns it, so one id finds a request everywhere.
 */
async function traceparentFor(): Promise<string> {
  try {
    const incoming = (await requestHeaders()).get("traceparent")?.toLowerCase()
    if (incoming && TRACEPARENT.test(incoming)) return `00-${incoming.slice(3, 35)}-${hex(8)}-01`
  } catch {
    // Outside a request (build time): start a fresh trace.
  }
  return `00-${hex(16)}-${hex(8)}-01`
}

export async function backendHeaders(extra?: HeadersInit): Promise<Headers> {
  const headers = new Headers(extra)
  if (!headers.has("traceparent")) headers.set("traceparent", await traceparentFor())
  const session = await sessionToken()
  if (session) {
    headers.set("Authorization", `Bearer ${session}`)
  } else if (process.env.DASHBOARD_SERVICE_KEY === "1" && process.env.BRAHMASTRA_API_KEY) {
    headers.set("Authorization", `Bearer ${process.env.BRAHMASTRA_API_KEY}`)
  }
  return headers
}

/** fetch() against the backend as the signed-in user. Server only. */
export async function backendFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${BACKEND_URL}${path.startsWith("/") ? path : `/${path}`}`
  return fetch(url, { ...init, headers: await backendHeaders(init.headers) })
}

/**
 * Whether a state-changing request came from this dashboard's own pages.
 *
 * The session lives in a cookie, and a browser sends cookies with requests
 * that OTHER sites trigger (a hidden form posting to /api/notes). SameSite=Lax
 * already stops most of those; checking that Origin names this host stops the
 * rest. A request with no Origin at all is refused for the same reason.
 */
export function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin")
  if (!origin) return false
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host")
  try {
    return new URL(origin).host === host
  } catch {
    return false
  }
}

/** Secure cookies only over HTTPS, or local http://localhost would never log in. */
export function isHttps(request: Request): boolean {
  const proto = request.headers.get("x-forwarded-proto")
  return proto ? proto.split(",")[0].trim() === "https" : new URL(request.url).protocol === "https:"
}
