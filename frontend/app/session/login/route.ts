/**
 * Sign in: checks the credentials with the backend and keeps the session in an
 * httpOnly cookie, so browser JavaScript (and anything injected into the page)
 * can never read the token.
 */
import { BACKEND_URL, SESSION_COOKIE, isHttps, sameOrigin } from "@/lib/backend"

export const dynamic = "force-dynamic"

export async function POST(request: Request) {
  if (!sameOrigin(request)) {
    return Response.json({ detail: "Cross-site request refused." }, { status: 403 })
  }
  let body: { email?: string; password?: string }
  try {
    body = await request.json()
  } catch {
    return Response.json({ detail: "Send email and password." }, { status: 400 })
  }
  let upstream: Response
  try {
    upstream = await fetch(`${BACKEND_URL}/auth/login`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // The backend's lockout counts failures per address, so pass the user's on.
        "X-Forwarded-For": request.headers.get("x-forwarded-for") ?? "",
        "User-Agent": request.headers.get("user-agent") ?? "",
      },
      body: JSON.stringify({ email: body.email ?? "", password: body.password ?? "" }),
    })
  } catch (error) {
    return Response.json({ detail: `Backend unreachable: ${(error as Error).message}` }, { status: 502 })
  }
  const data = await upstream.json().catch(() => ({}))
  if (!upstream.ok) {
    return Response.json({ detail: data.detail ?? "Sign-in failed." }, { status: upstream.status })
  }
  const maxAge = Math.max(60, Math.floor(data.expires_at - Date.now() / 1000))
  const cookie = [
    `${SESSION_COOKIE}=${data.token}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${maxAge}`,
    ...(isHttps(request) ? ["Secure"] : []),
  ].join("; ")
  return Response.json({ user: data.user }, { headers: { "Set-Cookie": cookie } })
}
