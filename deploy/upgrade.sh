#!/usr/bin/env bash
# Upgrade to a released version, with a backup first and an automatic rollback
# if the new version does not come up healthy.
#
#   ./upgrade.sh 0.3.0          upgrade to the git tag v0.3.0
#   ./upgrade.sh --rollback     go back to the version before the last upgrade
#
# Steps:
#   1. preflight: one upgrade at a time, clean checkout, the tag exists, valid config
#   2. backup now -- an upgrade without a fresh backup does not start
#   3. check out the tag and build images tagged with the new version
#   4. switch BRAHMASTRA_VERSION and restart; the backend applies its schema
#      migrations on start (they are additive, see docs/OPERATIONS.md)
#   5. health check; on failure, put the previous version back and check again
#
# Rolling back the CODE is safe without rolling back the DATA because every
# migration is backward compatible (expand now, contract in a later release):
# the previous version still runs on the migrated schema. The pre-upgrade
# backup is there for the case where that rule was broken.
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

HISTORY="${DEPLOY_DIR}/upgrade-history.log"
LOCK="${DEPLOY_DIR}/.upgrade.lock"

[ $# -eq 1 ] || die "usage: ./upgrade.sh <version> | --rollback"
[ -f "${ENV_FILE}" ] || die "no deploy/.env.production: run ./setup.sh first"

mkdir "${LOCK}" 2>/dev/null || die "another upgrade is running (or crashed: remove ${LOCK} if you are sure)"
trap 'rmdir "${LOCK}" 2>/dev/null || true' EXIT

notify() {
  local url
  url="$(env_get ALERT_WEBHOOK_URL)"
  [ -n "${url}" ] || return 0
  curl -fsS -m 15 -H 'Content-Type: application/json' \
    -d "{\"text\":\"[$(env_get DOMAIN)] $*\"}" "${url}" >/dev/null 2>&1 || true
}

previous="$(current_version)"
previous_ref="$(git -C "${REPO_DIR}" rev-parse HEAD)"

if [ "$1" = "--rollback" ]; then
  [ -f "${HISTORY}" ] || die "no upgrade history to roll back"
  target="$(awk '$3 == "upgraded" {print $4}' "${HISTORY}" | tail -n 1)"
  [ -n "${target}" ] || die "no earlier version recorded"
  info "rolling back ${previous} -> ${target}"
else
  target="${1#v}"
fi
tag="v${target}"
[ "${target}" != "${previous}" ] || die "already running ${target}"

# ---- 1. preflight ------------------------------------------------------------------------
git -C "${REPO_DIR}" diff --quiet && git -C "${REPO_DIR}" diff --cached --quiet \
  || die "the checkout has local changes; an upgrade must build exactly the released code"
git -C "${REPO_DIR}" fetch --quiet --tags origin || warn "could not fetch tags; using the tags present locally"
git -C "${REPO_DIR}" rev-parse --verify --quiet "${tag}^{commit}" >/dev/null || die "no release tag ${tag}"
"${COMPOSE[@]}" config --quiet || die "deploy/docker-compose.prod.yml or .env.production is invalid"
free_gb=$(df -Pk "${DEPLOY_DIR}" | awk 'NR==2 {printf "%d", $4/1024/1024}')
[ "${free_gb}" -ge 5 ] || die "only ${free_gb} GB free: an upgrade needs room for new images and a backup"
ok "preflight passed: ${previous} -> ${target}"

# ---- 2. backup ---------------------------------------------------------------------------
info "taking a backup before upgrading"
"${COMPOSE[@]}" run --rm -T backup backup || die "backup failed, upgrade NOT started"
ok "backup taken"

# ---- 3. build ------------------------------------------------------------------------------
git -C "${REPO_DIR}" -c advice.detachedHead=false checkout --quiet "${tag}"
info "building ${target}"
if ! BRAHMASTRA_VERSION="${target}" compose_build --pull --quiet; then
  git -C "${REPO_DIR}" -c advice.detachedHead=false checkout --quiet "${previous_ref}"
  die "build failed; still running ${previous}, nothing was changed"
fi

# ---- 4 + 5. switch, check, roll back on failure ----------------------------------------
env_set BRAHMASTRA_VERSION "${target}"
"${COMPOSE[@]}" up -d --remove-orphans
info "waiting for ${target} to become healthy"
if wait_healthy 300; then
  printf '%s %s upgraded %s -> %s\n' "$(date -u +%FT%TZ)" "$(whoami)" "${previous}" "${target}" >> "${HISTORY}"
  ok "${BOLD}now running ${target}${OFF}"
  notify "Upgraded Brahmastra ${previous} -> ${target}"
  exit 0
fi

warn "${target} did not become healthy; rolling back to ${previous}"
"${COMPOSE[@]}" logs --tail 50 backend >&2 || true
env_set BRAHMASTRA_VERSION "${previous}"
git -C "${REPO_DIR}" -c advice.detachedHead=false checkout --quiet "${previous_ref}"
if ! docker image inspect "brahmastra-backend:${previous}" >/dev/null 2>&1; then
  compose_build --quiet
fi
"${COMPOSE[@]}" up -d --remove-orphans
if wait_healthy 300; then
  printf '%s %s rolled-back %s -> %s\n' "$(date -u +%FT%TZ)" "$(whoami)" "${target}" "${previous}" >> "${HISTORY}"
  notify "Upgrade to ${target} FAILED its health check and was rolled back to ${previous}."
  die "upgrade to ${target} failed and was rolled back; ${previous} is running and healthy"
fi
notify "Upgrade to ${target} failed AND the rollback to ${previous} is unhealthy. Manual action needed."
die "rollback to ${previous} is not healthy either. Restore the pre-upgrade backup (docs/OPERATIONS.md)."
