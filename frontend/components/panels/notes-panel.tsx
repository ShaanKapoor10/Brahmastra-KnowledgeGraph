"use client"

import { useEffect, useMemo, useState, useTransition } from "react"
import type { Note, RawTriple } from "@/lib/types"
import { extractTriples } from "@/app/actions/extract"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { RELATION_LABEL } from "@/lib/viz"
import { FileText, Loader2, Plus, Search, Trash2, X } from "lucide-react"
import { PlanLocked } from "@/components/plan-lock"

interface Props {
  notes: Note[]
  triples: RawTriple[]
  onAddNote: (note: Note, triples: RawTriple[]) => void
  /** Called once the backend has removed a note. */
  onRemoved?: (id: string) => void
  /** Whether to offer Remove on a note added by `owner`. The server decides regardless. */
  canRemove?: (owner: string | null | undefined) => boolean
  /** False when the person's plan has no "Add notes": the form becomes a notice. */
  canAdd?: boolean
  workspace?: string
  backendAvailable?: boolean
}

/** A search hit as GET /notes?q= returns it: relevance order, never re-sorted. */
interface Hit {
  id: string
  title: string
  content: string
  last_edited?: string | null
  extraction_status?: string | null
  added_by?: string | null
}

export function NotesPanel({ notes, triples, onAddNote, onRemoved, canRemove, workspace = "default", backendAvailable = false, canAdd = true }: Props) {
  const [title, setTitle] = useState("")
  const [content, setContent] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, startTransition] = useTransition()

  const triplesByNote = (id: string) => triples.filter((t) => t.sourceNoteId === id)

  const [query, setQuery] = useState("")
  // What the backend last returned, and for which query: results for an older
  // query are never shown under a newer one.
  const [remote, setRemote] = useState<{ q: string; hits: Note[] } | null>(null)
  const [searching, setSearching] = useState(false)
  const [failed, setFailed] = useState<{ q: string; message: string } | null>(null)

  // The backend's HYBRID search (BM25 + vectors), so a note sharing no words
  // with the query still turns up. Offline, a plain substring filter.
  const q = query.trim()
  useEffect(() => {
    if (!q || !backendAvailable) return
    const ctrl = new AbortController()
    const t = setTimeout(async () => {
      setSearching(true)
      try {
        const scope = workspace === "default" ? "" : `&workspace=${encodeURIComponent(workspace)}`
        const res = await fetch(`/api/notes?q=${encodeURIComponent(q)}&limit=30${scope}`, {
          cache: "no-store",
          signal: ctrl.signal,
        })
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        const body: Hit[] = await res.json()
        setRemote({ q, hits:
          body.map((h) => ({
            id: h.id,
            title: h.title,
            content: h.content,
            lastEdited: h.last_edited ?? "",
            extractionStatus: (h.extraction_status ?? "done") as Note["extractionStatus"],
            addedBy: h.added_by ?? null,
          })),
        })
        setFailed(null)
      } catch (e) {
        if ((e as Error).name !== "AbortError") setFailed({ q, message: (e as Error).message })
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => {
      clearTimeout(t)
      ctrl.abort()
    }
  }, [q, backendAvailable, workspace])

  // Derived during render: nothing to search, the offline filter, or the
  // backend's answer for exactly this query.
  const offlineHits = useMemo(() => {
    if (!q || backendAvailable) return null
    const needle = q.toLowerCase()
    return notes.filter((n) => `${n.title} ${n.content}`.toLowerCase().includes(needle))
  }, [q, backendAvailable, notes])
  const hits: Note[] | null = !q ? null : backendAvailable ? (remote?.q === q ? remote.hits : null) : offlineHits
  const searchError = q && failed?.q === q ? failed.message : null

  const newestFirst = useMemo(
    () => [...notes].sort((a, b) => (b.lastEdited || "").localeCompare(a.lastEdited || "")),
    [notes],
  )
  // A note removed while a search is on screen leaves that search's results too.
  const [gone, setGone] = useState<Set<string>>(() => new Set())
  const shown = (hits ?? newestFirst).filter((n) => !gone.has(n.id))
  const scope = workspace === "default" ? "" : `?workspace=${encodeURIComponent(workspace)}`

  const removed = (id: string) => {
    setGone((g) => new Set(g).add(id))
    onRemoved?.(id)
  }

  const handleExtract = () => {
    if (!content.trim()) return
    setError(null)
    const id = `user-${Date.now()}`
    startTransition(async () => {
      const res = await extractTriples(id, content.trim())
      if (!res.ok) {
        setError(res.error ?? "Extraction failed")
        return
      }
      const note: Note = {
        id,
        title: title.trim() || "Untitled note",
        content: content.trim(),
        lastEdited: new Date().toISOString(),
        extractionStatus: "done",
      }
      onAddNote(note, res.triples)
      setTitle("")
      setContent("")
    })
  }

  return (
    <div className="flex flex-col gap-5">
      {!canAdd ? <PlanLocked feature="add_notes" /> : (
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-3 flex items-center gap-2">
          <Plus className="h-4 w-4 text-primary" />
          <h3 className="text-sm font-medium text-foreground">Add a note & extract live</h3>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">
          The note is sent to an ontology-constrained LLM (via the AI Gateway). The returned triples flow through entity
          resolution and the graph rebuilds instantly.
        </p>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Note title"
          className="mb-2 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
        />
        <Textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="e.g. Priya owns the search revamp. The search revamp depends on the auth migration and is scheduled for May 10."
          rows={4}
          className="mb-3 resize-none bg-background font-mono text-sm"
        />
        {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
        <Button onClick={handleExtract} disabled={pending || !content.trim()} className="gap-2">
          {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          {pending ? "Extracting…" : "Extract triples"}
        </Button>
      </div>
      )}

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 rounded-md border border-input bg-background px-3 py-2 focus-within:border-primary/60">
          {searching ? (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
          ) : (
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search notes by meaning, not just words"
            aria-label="Search notes"
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          {query && (
            <button onClick={() => setQuery("")} aria-label="Clear search" className="text-muted-foreground hover:text-foreground">
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <p className="font-mono text-[11px] text-muted-foreground">
          {hits
            ? `${hits.length} match${hits.length === 1 ? "" : "es"}, most relevant first${backendAvailable ? "" : " (offline: exact words only)"}`
            : `${notes.length} notes, newest first`}
        </p>
        {searchError && <p className="text-sm text-destructive">Search failed: {searchError}</p>}
      </div>

      <div className="flex flex-col gap-2.5">
        {shown.map((n) => (
          <NoteCard
            key={n.id}
            note={n}
            triples={triplesByNote(n.id)}
            removeUrl={canRemove?.(n.addedBy) ? `/api/notes/${encodeURIComponent(n.id)}${scope}` : null}
            onRemoved={() => removed(n.id)}
          />
        ))}
        {hits && hits.length === 0 && !searching && <p className="text-sm text-muted-foreground">No note matches that.</p>}
      </div>
    </div>
  )
}

function NoteCard({
  note,
  triples,
  removeUrl,
  onRemoved,
}: {
  note: Note
  triples: RawTriple[]
  /** Where to send DELETE, or null when this person may not remove the note. */
  removeUrl: string | null
  onRemoved: () => void
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [removeError, setRemoveError] = useState<string | null>(null)

  const remove = async () => {
    if (!removeUrl) return
    setRemoving(true)
    setRemoveError(null)
    try {
      const res = await fetch(removeUrl, { method: "DELETE", cache: "no-store" })
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(typeof body?.detail === "string" ? body.detail : `${res.status} ${res.statusText}`)
      }
      onRemoved()
    } catch (e) {
      setRemoveError((e as Error).message)
      setRemoving(false)
    }
  }

  const long = note.content.length > 360
  const status = note.extractionStatus as string
  return (
    <div className="rounded-lg border border-border bg-card p-3.5">
      <div className="mb-1.5 flex items-center gap-2">
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">{note.title}</span>
        {status && status !== "done" && (
          <span
            className={`rounded-full border px-1.5 py-0.5 font-mono text-[10px] ${
              status === "pending" ? "border-border text-muted-foreground" : "border-destructive/40 text-destructive"
            }`}
          >
            {status}
          </span>
        )}
        <span className="ml-auto shrink-0 font-mono text-[10px] uppercase tracking-wider text-primary">
          {triples.length} triple{triples.length === 1 ? "" : "s"}
        </span>
        {removeUrl && !confirming && (
          <button
            onClick={() => setConfirming(true)}
            aria-label={`Remove ${note.title}`}
            title="Remove this note"
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {confirming && (
        <div role="alert" className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 p-2.5 text-sm">
          <p className="text-foreground">
            Remove this note and the {triples.length > 0 ? `${triples.length} ` : ""}fact{triples.length === 1 ? "" : "s"} taken from it?
            The graph rebuilds without it in a minute or two. This can&rsquo;t be undone, apart from restoring a backup.
          </p>
          {removeError && <p className="mt-1.5 text-destructive">{removeError}</p>}
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="destructive" onClick={remove} disabled={removing} className="gap-1.5">
              {removing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              {removing ? "Removing…" : "Remove note"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => { setConfirming(false); setRemoveError(null) }} disabled={removing}>
              Keep it
            </Button>
          </div>
        </div>
      )}
      <p className={`whitespace-pre-line text-sm leading-relaxed text-muted-foreground ${open ? "" : "line-clamp-4"}`}>
        {note.content}
      </p>
      {(long || triples.length > 0) && (
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="mt-1.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          {open ? "less" : triples.length > 0 ? `more, with ${triples.length} extracted fact${triples.length === 1 ? "" : "s"}` : "more"}
        </button>
      )}
      {open && triples.length > 0 && (
        <div className="mt-2.5 flex flex-col gap-1">
          {triples.map((t) => (
            <div key={t.id} className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
              <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.subjectText}</span>
              <span className="text-primary">{RELATION_LABEL[t.relation] ?? t.relation}</span>
              <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.objectText}</span>
              <span className="text-muted-foreground/60">·{t.confidence.toFixed(2)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
