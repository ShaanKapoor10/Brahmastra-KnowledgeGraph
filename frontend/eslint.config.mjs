// ESLint flat config: Next.js's core-web-vitals rules plus TypeScript.
// Run: pnpm lint (CI runs it on every pull request; errors fail the build).
// React's compiler-oriented hook rules (set-state-in-effect, refs) gate too:
// the 13 places that broke them were restructured (docs/ROADMAP.md item 14).
import nextVitals from "eslint-config-next/core-web-vitals"
import nextTs from "eslint-config-next/typescript"

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "components/ui/**"] },
]

export default config
