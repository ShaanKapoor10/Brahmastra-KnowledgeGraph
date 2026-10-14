import type { Note, RawTriple } from "./types"

/**
 * What a PRODUCTION build gets in place of lib/sample-notes.ts (aliased in
 * next.config.mjs). No sample data, and a flag the startup guard checks.
 */
export const IS_DEMO_BUILD = false
export const SAMPLE_DATA_MARKER = ""
export const SAMPLE_NOTES: Note[] = []
export const SAMPLE_TRIPLES: RawTriple[] = []
