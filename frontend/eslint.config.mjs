// ESLint flat config: Next.js's core-web-vitals rules plus TypeScript.
// Run: pnpm lint (CI runs it on every pull request; errors fail the build).
import nextVitals from "eslint-config-next/core-web-vitals"
import nextTs from "eslint-config-next/typescript"

const config = [
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // Reported, not yet gating: React's newer compiler-oriented hook rules
      // flag 13 existing places (setState in an effect, ref reads in render).
      // Each needs a component restructure, which is behaviour change and
      // belongs in its own reviewed PR, not in a CI rollout. Tracked in
      // docs/ROADMAP.md; turn these to "error" once that lands.
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/refs": "warn",
    },
  },
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "components/ui/**"] },
]

export default config
