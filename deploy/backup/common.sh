#!/usr/bin/env bash
# Shared by backup.sh, restore-test.sh and restore.sh. Never echoes a secret.
set -Eeuo pipefail
umask 077

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${JOB:-backup}" "$*" >&2; }

# Post to ALERT_WEBHOOK_URL ({"text": ...} works for Slack and Teams).
alert() {
  local msg="[${INSTANCE_NAME:-brahmastra}] $*"
  log "ALERT: $*"
  [ -n "${ALERT_WEBHOOK_URL:-}" ] || return 0
  local body
  body=$(printf '%s' "$msg" | sed 's/\/\\/g; s/"/\\"/g')
  curl -fsS -m 15 -H 'Content-Type: application/json' -d "{\"text\":\"${body}\"}" \
    "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || log "could not reach the alert webhook"
}

# Dead-man's switch: ping on success; "$url/fail" on failure (healthchecks.io style).
heartbeat() {
  local url="${1:-}" outcome="${2:-ok}"
  [ -n "$url" ] || return 0
  [ "$outcome" = ok ] || url="${url%/}/fail"
  curl -fsS -m 10 --retry 3 "$url" >/dev/null 2>&1 || log "could not reach the heartbeat URL"
}

offsite_enabled() {
  [ "${BACKUP_OFFSITE:-on}" != "off" ] && [ -n "${RESTIC_REPOSITORY:-}" ]
}

require_offsite_config() {
  if [ "${BACKUP_OFFSITE:-on}" = "off" ]; then
    log "WARNING: BACKUP_OFFSITE=off: dumps stay on this host only."
    return 0
  fi
  if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
    log "RESTIC_REPOSITORY and RESTIC_PASSWORD are required for the off-host copy (or set BACKUP_OFFSITE=off)."
    return 1
  fi
}
