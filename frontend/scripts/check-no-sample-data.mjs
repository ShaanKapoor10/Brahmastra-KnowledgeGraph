/**
 * Fails when a production build contains the sample data. Run after `next build`
 * (the Dockerfile does), so an image that would ship sample notes is never made.
 */
import { readdirSync, readFileSync } from "node:fs"
import { join } from "node:path"

const MARKERS = ["brahmastra-sample-data-v1", "PromptlyBI"]
const roots = [".next/server", ".next/static", ".next/standalone/.next/server"]
const hits = []
function walk(dir) {
  let entries
  // Entry types come with the listing: no separate stat, so nothing can change
  // between checking a path and reading it.
  try { entries = readdirSync(dir, { withFileTypes: true }) } catch { return }
  for (const entry of entries) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) walk(path)
    else if (entry.isFile() && /\.(js|mjs|cjs|json|html|rsc|txt)$/.test(entry.name)) {
      const text = readFileSync(path, "utf8")
      for (const m of MARKERS) if (text.includes(m)) hits.push(`${path}: ${m}`)
    }
  }
}
roots.forEach(walk)
if (process.env.BRAHMASTRA_DEMO_BUILD === "1") {
  console.log(`demo build: sample data expected (${hits.length} references)`)
} else if (hits.length) {
  console.error("Sample data found in a production build:\n  " + hits.slice(0, 10).join("\n  "))
  process.exit(1)
} else {
  console.log("ok: no sample data in this build")
}
