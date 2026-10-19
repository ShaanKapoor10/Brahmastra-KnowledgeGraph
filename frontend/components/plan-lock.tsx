import { Lock } from "lucide-react"
import { FEATURE_NAMES } from "@/lib/plans"

/** Shown in place of a control the person's plan does not include. */
export function PlanLocked({ feature, compact = false }: { feature: string; compact?: boolean }) {
  const name = FEATURE_NAMES[feature] ?? feature
  if (compact) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Lock className="h-3 w-3" /> {name} is not in your plan
      </span>
    )
  }
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-card p-4">
      <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="flex flex-col gap-0.5">
        <p className="text-sm font-medium text-foreground">{name} is not in your plan</p>
        <p className="text-sm text-muted-foreground">Ask an admin to move you to a plan that includes it.</p>
      </div>
    </div>
  )
}
