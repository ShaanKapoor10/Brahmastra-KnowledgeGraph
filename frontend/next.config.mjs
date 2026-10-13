/** @type {import('next').NextConfig} */
const nextConfig = {
  // Emit .next/standalone: a self-contained server with only the node_modules
  // it actually imports. Without it the runtime image has to carry the full
  // dependency tree, which is most of the image size for a Next app.
  output: "standalone",
  // A type error fails the build. It used to be ignored, which ships whatever
  // the error was pointing at.
  typescript: {
    ignoreBuildErrors: false,
  },
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ]
  },
  images: {
    unoptimized: true,
  },
  // No /api rewrite here on purpose: it forwards requests untouched and
  // cannot attach an Authorization header, so it 401s the moment the API
  // requires a key. app/api/[...path]/route.ts proxies instead, sending the
  // signed-in user's session from an httpOnly cookie.
}

export default nextConfig
