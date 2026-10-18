/**
 * Startup guard, the second layer behind the build-time alias: a production
 * server refuses to start if it was built with the sample data in it.
 * (scripts/check-no-sample-data.mjs is the first layer; it fails the image build.)
 */
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { refuseDemoBuildInProduction } = await import("./instrumentation-node")
    await refuseDemoBuildInProduction()
  }
}
