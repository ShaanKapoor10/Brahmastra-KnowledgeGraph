/**
 * The Node.js half of the startup guard (instrumentation.ts loads it only on
 * the Node runtime). It lives in its own module because process.exit does not
 * exist in the Edge runtime, and Next compiles instrumentation.ts for both.
 */
export async function refuseDemoBuildInProduction(): Promise<void> {
  if (process.env.BRAHMASTRA_ENV !== "production") return
  const { IS_DEMO_BUILD } = await import("@/lib/sample-notes")
  if (IS_DEMO_BUILD) {
    console.error("[startup] Refusing to start: this build contains sample data (a demo build). " +
      "Rebuild without BRAHMASTRA_DEMO_BUILD=1 for production.")
    process.exit(78)
  }
}
