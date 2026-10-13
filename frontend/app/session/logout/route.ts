/** Sign out: ends the session on the backend, then clears the cookie. */
import { backendFetch, SESSION_COOKIE, isHttps, sameOrigin } from "@/lib/backend"

export const dynamic = "force-dynamic"

export async function POST(request: Request) {
  if (!sameOrigin(request)) {
    return Response.json({ detail: "Cross-site request refused." }, { status: 403 })
  }
  // Ending it server-side matters: clearing only the cookie would leave a
  // stolen copy of the token valid until it expired.
  await backendFetch("/auth/logout", { method: "POST" }).catch(() => undefined)
  const cookie = [`${SESSION_COOKIE}=`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0",
    ...(isHttps(request) ? ["Secure"] : [])].join("; ")
  return new Response(null, { status: 204, headers: { "Set-Cookie": cookie } })
}
