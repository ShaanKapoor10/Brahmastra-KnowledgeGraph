#!/usr/bin/env bash
# Upgrade-path test: a running install of the PREVIOUS release, with data in
# it, is upgraded to THIS build -- schema migrations included -- and must come
# up healthy with its data intact. This is what the backward-compatible
# migration rule (docs/OPERATIONS.md) promises; this test is what enforces it.
#
#   NEW_BACKEND_IMAGE=... NEW_FRONTEND_IMAGE=... NEW_BACKUP_IMAGE=... deploy/tests/upgrade-path-test.sh
#
# The previous release is the newest v* tag that is an ancestor of HEAD; its
# images are built from that tag's source here, so the test needs no registry
# access. With no earlier release (the first one), it says so and passes.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

prev="$(git describe --tags --abbrev=0 --match 'v[0-9]*' HEAD^ 2>/dev/null || true)"
if [ -z "${prev}" ]; then
  echo "no earlier release tag: nothing to upgrade from (first release) -- skipping"
  exit 0
fi
echo "upgrading from ${prev} to $(git rev-parse --short HEAD)"

work="$(mktemp -d)"
project="brahmastra-upgrade-$$"
env_file="$(mktemp deploy/.env.upgrade.XXXXXX)"   # relative, beside the compose files; gitignored
cleanup() {
  docker compose -p "${project}" -f deploy/docker-compose.prod.yml --env-file "${env_file}" down -v --remove-orphans >/dev/null 2>&1 || true
  git worktree remove --force "${work}/prev" >/dev/null 2>&1 || true
  rm -rf "${work}" "${env_file}"
}
trap cleanup EXIT

# ---- the previous release, built from its own source ---------------------------------------
git worktree add -q --detach "${work}/prev" "${prev}"
for svc in backend frontend; do
  docker build -q -t "brahmastra-${svc}:upgrade-prev" "${work}/prev/${svc}" >/dev/null
done
docker build -q -t brahmastra-backup:upgrade-prev "${work}/prev/deploy/backup" >/dev/null

secret() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40; }
grep -vE '^(POSTGRES_PASSWORD|NEO4J_PASSWORD|GRAFANA_ADMIN_PASSWORD|COMPOSE_PROFILES|DOMAIN|PUBLIC_URL|CADDY_TLS|HTTP_PORT|HTTPS_PORT|BACKUP_OFFSITE|NEO4J_HEAP|NEO4J_PAGECACHE|NEO4J_MEMORY|BACKEND_MEMORY)=' \
  deploy/.env.production.example > "${env_file}"
cat >> "${env_file}" <<EOF
POSTGRES_PASSWORD=$(secret)
NEO4J_PASSWORD=$(secret)
GRAFANA_ADMIN_PASSWORD=$(secret)
COMPOSE_PROFILES=
DOMAIN=localhost
PUBLIC_URL=https://localhost:19443
CADDY_TLS=internal
HTTP_PORT=19080
HTTPS_PORT=19443
BACKUP_OFFSITE=off
BACKEND_MEMORY=1500m
NEO4J_MEMORY=1g
NEO4J_HEAP=256m
NEO4J_PAGECACHE=256m
EOF

# The previous release runs with ITS OWN compose file: that is what an install of it has.
prev_compose=(docker compose -p "${project}" -f "${work}/prev/deploy/docker-compose.prod.yml" --env-file "${env_file}")
new_compose=(docker compose -p "${project}" -f deploy/docker-compose.prod.yml --env-file "${env_file}")

# BRAHMASTRA_VERSION too: compose files before 0.3 name images only by version.
BRAHMASTRA_VERSION=upgrade-prev BACKEND_IMAGE=brahmastra-backend:upgrade-prev \
FRONTEND_IMAGE=brahmastra-frontend:upgrade-prev BACKUP_IMAGE=brahmastra-backup:upgrade-prev \
  "${prev_compose[@]}" up -d --wait --wait-timeout 420 >/dev/null
echo "ok    ${prev} is running"

# ---- data, written by the previous release ----------------------------------------------------
"${prev_compose[@]}" exec -T backend python -c "
from brahmastra import db
db.upsert_note('upgrade-probe', 'Upgrade probe', 'Written by the previous release; must survive the upgrade.')
print('note written')" >/dev/null
printf 'Upgrade-test-password-1\n' | "${prev_compose[@]}" exec -T backend \
  python -m brahmastra.accounts create-admin --email upgrade@example.com --password-stdin >/dev/null
echo "ok    data written by ${prev}"

# ---- upgrade: same volumes, new images and compose file --------------------------------------
BACKEND_IMAGE="${NEW_BACKEND_IMAGE:-brahmastra-backend:smoke}" FRONTEND_IMAGE="${NEW_FRONTEND_IMAGE:-brahmastra-frontend:smoke}" \
BACKUP_IMAGE="${NEW_BACKUP_IMAGE:-brahmastra-backup:smoke}" "${new_compose[@]}" up -d --wait --wait-timeout 420 --remove-orphans >/dev/null
echo "ok    upgraded stack is healthy"

"${new_compose[@]}" exec -T backend python -c "
import sys
from brahmastra import db
from brahmastra.accounts import store
note = db.get_note('upgrade-probe')
assert note and 'survive the upgrade' in note['content'], 'the note did not survive'
assert store().find_user('upgrade@example.com'), 'the account did not survive'
print('data intact')" && echo "ok    data intact after the upgrade"
