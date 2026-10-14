/**
 * Startup guard, the second layer behind the build-time alias: a production
 * server refuses to start if it was built with the sample data in it.
 * (scripts/check-no-sample-data.mjs is the first layer; it fails the image build.)
 */
export async function register() {
  if (process.env.BRAHMASTRA_ENV !== "production") return
  const { IS_DEMO_BUILD } = await import("@/lib/sample-notes")
  if (IS_DEMO_BUILD) {
    console.error("[startup] Refusing to start: this build contains sample data (a demo build). " +
      "Rebuild without BRAHMASTRA_DEMO_BUILD=1 for production.")
    process.exit(78)
  }
}
