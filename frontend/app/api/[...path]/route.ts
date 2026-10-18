/**
 * Proxies /api/* to the Brahmastra backend, as the signed-in user.
 *
 * Replaces the next.config.mjs rewrite. A rewrite forwards the request as-is
 * and cannot attach an Authorization header. Doing it as a route handler lets
 * the server read the httpOnly session cookie and send it on as a bearer
 * token, while browser code carries on calling /api/... unchanged.
 *
 * State-changing requests must come from this dashboard's own pages (see
 * sameOrigin in lib/backend.ts): the credential is a cookie, so without that
 * check another site could make a visitor's browser write into their graph.
 */
import { backendFetch, sameOrigin } from "@/lib/backend"

export const dynamic = "force-dynamic"

const SAFE = new Set(["GET", "HEAD"])

async function proxy(request: Request, path: string[]): Promise<Response> {
  if (!SAFE.has(request.method) && !sameOrigin(request)) {
    return Response.json({ detail: "Cross-site request refused." }, { status: 403 })
  }
  const target = `/${path.join("/")}${new URL(request.url).search}`

  const headers: Record<string, string> = {}
  const workspace = request.headers.get("x-brahmastra-workspace")
  if (workspace) headers["X-Brahmastra-Workspace"] = workspace
  const init: RequestInit = { method: request.method, headers }
  if (!SAFE.has(request.method)) {
    init.body = await request.text()
    headers["Content-Type"] = request.headers.get("content-type") ?? "application/json"
  }

  try {
    const upstream = await backendFetch(target, init)
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    })
  } catch (error) {
    // The backend being unreachable is an expected state -- Aura Free suspends
    // after a few days idle -- so say so rather than surfacing a stack trace.
    return Response.json(
      { detail: `Backend unreachable: ${(error as Error).message}` },
      { status: 502 },
    )
  }
}

type Ctx = { params: Promise<{ path: string[] }> }

export async function GET(request: Request, ctx: Ctx) {
  return proxy(request, (await ctx.params).path)
}
export async function POST(request: Request, ctx: Ctx) {
  return proxy(request, (await ctx.params).path)
}
export async function PATCH(request: Request, ctx: Ctx) {
  return proxy(request, (await ctx.params).path)
}
export async function PUT(request: Request, ctx: Ctx) {
  return proxy(request, (await ctx.params).path)
}
export async function DELETE(request: Request, ctx: Ctx) {
  return proxy(request, (await ctx.params).path)
}
