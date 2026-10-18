# Brahmastra frontend — Next.js dashboard.
#
# The route handler in app/api/[...path] proxies /api/* to the backend, so
# BACKEND_URL must be readable AT RUNTIME, not baked in at build. That is why
# it is an ENV here and not a build ARG: the same image then runs against a
# local backend, a staging one, and production.
#
# The pnpm version is pinned by `packageManager` in package.json, and corepack
# honours it. Leaving it unpinned made this image depend on whatever pnpm was
# newest at build time: it broke without a single line changing here when pnpm
# 11 shipped, because that release requires Node >= 22.13 for `node:sqlite`
# while the base was then Node 20. Bump the base image and the pin together.
#
# Node 24 (LTS) on Debian 13: Node 20 reached end of life in April 2026, and
# its Debian 12 base carried 17 fixable HIGH/CRITICAL findings (Trivy, 2026-10-19).

FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315 AS deps
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315 AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# next build reads BACKEND_URL for the rewrite default; the runtime value below
# is what actually takes effect, this only has to be syntactically valid.
ENV NEXT_TELEMETRY_DISABLED=1
# Sample notes ship only in a demo build (BRAHMASTRA_DEMO_BUILD=1); see lib/sample-notes.ts.
ARG BRAHMASTRA_DEMO_BUILD=
ENV BRAHMASTRA_DEMO_BUILD=${BRAHMASTRA_DEMO_BUILD}
RUN pnpm build
# A production image must not contain them: this fails the image build if it does.
RUN node scripts/check-no-sample-data.mjs

FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315 AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    BACKEND_URL=http://backend:8001

# The runtime only ever runs `node server.js`. npm, npx and corepack ship with
# the base image, are never used here, and carried its remaining CVEs.
RUN groupadd --system --gid 10001 nextjs \
 && useradd --system --create-home --uid 10001 --gid 10001 nextjs \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
           /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

# standalone carries its own minimal server.js plus the node_modules it needs.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

USER 10001:10001
EXPOSE 3000

# Next's standalone server binds to whatever HOSTNAME says, and Docker sets
# HOSTNAME to the container id. Left alone it therefore listened only on the
# container's own eth0 address: published traffic worked, so the dashboard
# looked fine, while the loopback healthcheck below got ECONNREFUSED and the
# container sat permanently unhealthy. Anything gating on that health -- a
# depends_on, a load balancer, a platform's deploy check -- would refuse to
# proceed against a server that was serving perfectly.
ENV HOSTNAME=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000),r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"]

CMD ["node", "server.js"]
