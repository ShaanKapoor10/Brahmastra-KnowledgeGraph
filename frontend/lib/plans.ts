/**
 * Plans, as the dashboard sees them. The backend decides every request
 * (brahmastra/plans.py); this only locks controls that would be refused anyway,
 * so a person sees "not in your plan" before typing, not after.
 */

export interface Plan {
  id: string
  name: string
  description: string | null
  features: string[]
  limits: Record<string, number | null>
}

export interface PlanCatalog {
  plans: Plan[]
  /** Feature id -> name people read. */
  features: Record<string, string>
  /** Limit id -> name people read. */
  limits: Record<string, string>
}

/** One limit's use, as GET /auth/usage returns it. */
export interface UsageRow {
  meter: "questions" | "notes" | "words" | "meeting_minutes" | "tokens"
  period: "day" | "month"
  limit_key: string
  /** Meeting minutes arrive already converted to hours. */
  used: number
  limit: number | null
  /** Seconds since the epoch. */
  resets_at: number
}

/** Names for features, for the moments before the catalog has loaded. */
export const FEATURE_NAMES: Record<string, string> = {
  ask: "Ask",
  browse: "Search and browse",
  graph: "Graph explorer",
  add_notes: "Add notes",
  mcp: "AI assistant access (MCP)",
  documents: "Document upload",
  meetings: "Meetings",
  cross_workspace: "Cross-workspace search",
}

/**
 * Whether a person's plan includes a feature. No user (development mode) and
 * features === null (the service key) mean every feature, as on the backend.
 */
export function can(user: { features?: string[] | null } | null | undefined, feature: string): boolean {
  if (!user || user.features == null) return true
  return user.features.includes(feature)
}

/**
 * Whether the Remove button should show for something added by `owner`.
 * Mirrors backend/brahmastra/removals.py: admins remove anything, a member only
 * what is recorded as theirs, a viewer nothing. The server decides regardless;
 * this only hides a button it would refuse. No user means development mode,
 * where the backend treats every request as an admin.
 */
export function mayRemove(
  user: { role?: string; user_id?: string | null } | null | undefined,
  owner: string | null | undefined,
): boolean {
  if (!user) return true
  if (user.role === "admin") return true
  return user.role === "member" && !!user.user_id && !!owner && owner === user.user_id
}
