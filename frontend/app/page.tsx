import { redirect } from "next/navigation"
import { Dashboard, type SignedInUser } from "@/components/dashboard"
import { IS_DEMO_BUILD, SAMPLE_NOTES, SAMPLE_TRIPLES } from "@/lib/sample-notes"
import type { Note, RawTriple, PipelineResult } from "@/lib/types"
import { adaptBackendGraph } from "@/lib/backend-adapter"
import { backendFetch } from "@/lib/backend"
import type { WorkspaceOption } from "@/components/workspace-switcher"
import type { BackendGraphResponse } from "@/lib/backend-adapter"

/** How the graph on screen relates to the live engine (see the banner in the dashboard). */
export type GraphStatus =
  | { state: "ok" }
  | { state: "stale"; builtAt: string | null }
  | { state: "unavailable" }

type Loaded =
  | { kind: "ok"; notes: Note[]; triples: RawTriple[]; result: PipelineResult | null; graph: GraphStatus }
  | { kind: "unreachable"; reason: string }

/**
 * Load the workspace from the backend.
 *
 * Notes come from the system of record and are REQUIRED: without them there
 * is nothing true to show, so the page says the server cannot be reached.
 * The graph is derived and OPTIONAL: when its engine is down the backend serves
 * the last good copy (marked stale), or nothing, and the notes still show with
 * a banner. There is no sample-data fallback in production builds -- invented
 * notes on a real install look like data loss (lib/sample-notes.ts).
 */
async function loadFromBackend(workspace: string): Promise<Loaded> {
  // Every request names the workspace it wants. The API resolves it per
  // request, so leaving it off silently returns whichever graph the server
  // process defaults to -- which is how the dashboard could only ever show one.
  const scope = workspace === "default" ? "" : `?workspace=${encodeURIComponent(workspace)}`

  let notesRes: Response
  try {
    notesRes = await backendFetch(`/notes${scope}`, { cache: "no-store" })
  } catch (error) {
    return { kind: "unreachable", reason: (error as Error).message }
  }
  if (!notesRes.ok) return { kind: "unreachable", reason: `the notes request returned ${notesRes.status}` }

  const backendNotes: Array<{
    id: string
    title: string
    content: string
    last_edited: string | null
    extraction_status: string
  }> = await notesRes.json()
  const notes: Note[] = backendNotes.map((n) => ({
    id: n.id,
    title: n.title,
    content: n.content,
    lastEdited: n.last_edited ?? n.id,
    extractionStatus: n.extraction_status as Note["extractionStatus"],
  }))

  const [triplesRes, graphRes] = await Promise.all([
    backendFetch(`/graph/triples${scope}`, { cache: "no-store" }).catch(() => null),
    backendFetch(`/graph${scope}`, { cache: "no-store" }).catch(() => null),
  ])

  let triples: RawTriple[] = []
  if (triplesRes?.ok) {
    const backendTriples: Array<{
      id: number
      relation: string
      subject_text: string
      object_text: string
      confidence: number
      source_quote: string | null
      source_note_id: string | null
      extracted_at: string
    }> = await triplesRes.json()
    triples = backendTriples.map((t) => ({
      id: String(t.id),
      subjectText: t.subject_text,
      relation: t.relation as RawTriple["relation"],
      objectText: t.object_text,
      confidence: t.confidence,
      sourceQuote: t.source_quote ?? "",
      sourceNoteId: t.source_note_id ?? "",
      extractedAt: t.extracted_at,
    }))
  }

  let graph: GraphStatus = { state: "unavailable" }
  let result: PipelineResult | null = null
  if (graphRes?.ok) {
    const payload: BackendGraphResponse & {
      availability?: { graph?: string; served_from?: string; snapshot_built_at?: string | null }
    } = await graphRes.json()
    graph = payload.availability?.served_from === "snapshot"
      ? { state: "stale", builtAt: payload.availability.snapshot_built_at ?? payload.built_at }
      : { state: "ok" }
    if (payload.graph.nodes.length > 0) result = adaptBackendGraph(payload, notes, triples)
  }
  return { kind: "ok", notes, triples, result, graph }
}

/** Workspaces the backend knows about. Empty when it is unreachable. */
async function loadWorkspaces(): Promise<WorkspaceOption[]> {
  try {
    const res = await backendFetch("/workspaces", { cache: "no-store" })
    if (!res.ok) return []
    const body: { workspaces?: WorkspaceOption[] } = await res.json()
    return body.workspaces ?? []
  } catch {
    return []
  }
}

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workspace?: string }>
}) {
  // The workspace lives in the URL so a selection is shareable and survives a
  // reload, and so the server component below actually re-renders with the
  // other graph rather than filtering one it already fetched.
  const workspace = (await searchParams).workspace?.trim() || "default"

  // Who is looking. 401: not signed in. 503: no accounts exist yet, and the
  // login page explains how to create the first admin. An unreachable backend
  // falls through, so the page still renders and says the backend is down.
  const meRes = await backendFetch("/auth/me", { cache: "no-store" }).catch(() => null)
  if (meRes && (meRes.status === 401 || meRes.status === 503)) {
    const next = workspace === "default" ? "/" : `/?workspace=${encodeURIComponent(workspace)}`
    redirect(`/login?next=${encodeURIComponent(next)}`)
  }
  const user: SignedInUser | null = meRes?.ok ? await meRes.json() : null

  const [loaded, workspaces] = await Promise.all([
    loadFromBackend(workspace),
    loadWorkspaces(),
  ])

  if (loaded.kind === "unreachable") {
    // A demo build (BRAHMASTRA_DEMO_BUILD=1) may still show its sample vault
    // with no backend at all; a production build has none to show.
    if (IS_DEMO_BUILD) {
      return (
        <Dashboard
          initialNotes={SAMPLE_NOTES}
          initialTriples={SAMPLE_TRIPLES}
          backendAvailable={false}
          initialResult={null}
          workspace={workspace}
          workspaces={[{ id: workspace }]}
          user={user}
        />
      )
    }
    return <Unreachable reason={loaded.reason} />
  }

  return (
    <Dashboard
      initialNotes={loaded.notes}
      initialTriples={loaded.triples}
      backendAvailable
      initialResult={loaded.result}
      graphStatus={loaded.graph}
      workspace={workspace}
      workspaces={workspaces.length > 0 ? workspaces : [{ id: workspace }]}
      user={user}
    />
  )
}

/** The system of record cannot be reached: say so plainly, never show invented data. */
function Unreachable({ reason }: { reason: string }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="max-w-md space-y-2 text-center">
        <h1 className="text-lg font-semibold">Brahmastra can&apos;t reach its server</h1>
        <p className="text-sm text-muted-foreground">
          Your notes are safe; the dashboard just can&apos;t load them right now. Try again in a minute. If it
          persists, ask your administrator to check the service.
        </p>
        <p className="font-mono text-[11px] text-muted-foreground">{reason}</p>
      </div>
    </main>
  )
}
