#!/usr/bin/env bash
# Smoke-test PRODUCTION images: start the real production compose stack from
# them and check the properties a release must have.
#
#   BACKEND_IMAGE=... FRONTEND_IMAGE=... BACKUP_IMAGE=... deploy/tests/smoke-test.sh
#   (defaults: brahmastra-{backend,frontend,backup}:smoke, as CI builds them)
#
# Checks:
#   1. every app container runs as a non-root user, with a read-only root filesystem
#   2. the dashboard image contains no sample data (lib/sample-notes.ts)
#   3. the startup guard refuses unsafe settings (exit 78)
#   4. /readyz reports ready, and /healthz answers through the edge (Caddy)
# Needs only Docker. Leaves nothing behind.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export BACKEND_IMAGE="${BACKEND_IMAGE:-brahmastra-backend:smoke}"
export FRONTEND_IMAGE="${FRONTEND_IMAGE:-brahmastra-frontend:smoke}"
export BACKUP_IMAGE="${BACKUP_IMAGE:-brahmastra-backup:smoke}"
project="brahmastra-smoke-$$"
# Relative, beside the compose file (gitignored as .env.*), not in /tmp: Docker on
# Windows cannot see a Git Bash /tmp path, and Linux does not care.
env_file="$(mktemp .env.smoke.XXXXXX)"
compose=(docker compose -p "${project}" -f docker-compose.prod.yml --env-file "${env_file}")
fail=0

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "${env_file}"
}
trap cleanup EXIT

secret() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40; }
{
  grep -vE '^(POSTGRES_PASSWORD|NEO4J_PASSWORD|GRAFANA_ADMIN_PASSWORD|COMPOSE_PROFILES|DOMAIN|PUBLIC_URL|CADDY_TLS|HTTP_PORT|HTTPS_PORT|BACKUP_OFFSITE|BACKEND_MEMORY|NEO4J_MEMORY|NEO4J_HEAP|NEO4J_PAGECACHE)=' .env.production.example
  echo "POSTGRES_PASSWORD=$(secret)"
  echo "NEO4J_PASSWORD=$(secret)"
  echo "GRAFANA_ADMIN_PASSWORD=$(secret)"
  echo "COMPOSE_PROFILES="
  echo "DOMAIN=localhost"
  echo "PUBLIC_URL=https://localhost:18443"
  echo "CADDY_TLS=internal"
  echo "HTTP_PORT=18080"
  echo "HTTPS_PORT=18443"
  echo "BACKUP_OFFSITE=off"
  echo "BACKEND_MEMORY=1500m"
  echo "NEO4J_MEMORY=1g"
  echo "NEO4J_HEAP=256m"
  echo "NEO4J_PAGECACHE=256m"
} > "${env_file}"

check() {   # description, then a command that must succeed
  local what="$1"; shift
  if "$@" >/dev/null 2>&1; then printf 'ok    %s\n' "${what}"; else printf 'FAIL  %s\n' "${what}"; fail=1; fi
}

# ---- 3. the startup guard, before anything else runs --------------------------------
set +e
docker run --rm -e BRAHMASTRA_ENV=production -e NOTE_BACKEND=postgres \
  -e POSTGRES_PASSWORD=brahmastra -e NEO4J_PASSWORD=brahmastra "${BACKEND_IMAGE}" >/dev/null 2>&1
code=$?
set -e
check "startup guard refuses default secrets (exit ${code}, want 78)" test "${code}" -eq 78

# ---- 2. no sample data in the dashboard image ----------------------------------------
check "dashboard image holds no sample data" \
  sh -c "! docker run --rm --entrypoint sh '${FRONTEND_IMAGE}' -c 'grep -rl brahmastra-sample-data-v1 /app 2>/dev/null' | grep -q ."

# ---- bring the stack up --------------------------------------------------------------------
"${compose[@]}" up -d --wait --wait-timeout 420 >/dev/null 2>&1 || {
  echo "FAIL  the stack did not become healthy"; "${compose[@]}" ps; "${compose[@]}" logs --tail 40 backend; exit 1; }
printf 'ok    stack healthy\n'

# ---- 1. non-root, read-only -------------------------------------------------------------
for svc in backend frontend caddy; do
  cid="$("${compose[@]}" ps -q "${svc}")"
  user="$(docker inspect -f '{{.Config.User}}' "${cid}")"
  ro="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "${cid}")"
  if [ "${svc}" = caddy ]; then
    check "caddy: read-only root filesystem" test "${ro}" = true
  else
    check "${svc}: runs as non-root user (${user:-root})" sh -c "[ -n '${user}' ] && [ '${user}' != root ] && [ '${user}' != 0 ]"
    check "${svc}: read-only root filesystem" test "${ro}" = true
  fi
done

# ---- 4. probes -------------------------------------------------------------------------------
check "/readyz reports ready" "${compose[@]}" exec -T backend python -c \
  "import json,urllib.request,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8001/readyz', timeout=10))['status']=='ready' else 1)"
check "/healthz answers through the edge" sh -c \
  "curl -sk --max-time 10 https://localhost:18443/healthz | grep -q '\"status\": *\"ok\"'"

exit "${fail}"
