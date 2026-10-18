#!/usr/bin/env bash
# Shared by backup.sh, restore-test.sh and restore.sh. Never echoes a secret.
set -Eeuo pipefail
umask 077

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${JOB:-backup}" "$*" >&2; }

# Escape a string for a JSON string literal: backslash first, then quotes,
# then the control characters a log message can carry.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' | tr '\n\r' '  '
}

# Post to ALERT_WEBHOOK_URL ({"text": ...} works for Slack and Teams).
alert() {
  local msg="[${INSTANCE_NAME:-brahmastra}] $*"
  log "ALERT: $*"
  [ -n "${ALERT_WEBHOOK_URL:-}" ] || return 0
  curl -fsS -m 15 -H 'Content-Type: application/json' -d "{\"text\":\"$(json_escape "$msg")\"}" \
    "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || log "could not reach the alert webhook"
}

# Dead-man's switch: ping on success; "$url/fail" on failure (healthchecks.io style).
heartbeat() {
  local url="${1:-}" outcome="${2:-ok}"
  [ -n "$url" ] || return 0
  [ "$outcome" = ok ] || url="${url%/}/fail"
  curl -fsS -m 10 --retry 3 "$url" >/dev/null 2>&1 || log "could not reach the heartbeat URL"
}

# The outcome, for the backend's metrics (brahmastra/metrics.py reads
# /ops-state): last attempt, whether it succeeded, and when it last did --
# carried forward across failures, so "how old is the newest good backup?"
# always has an answer an alert can test.
record_state() {
  local name="$1" outcome="$2" file now last_ok
  [ -d /ops-state ] && [ -w /ops-state ] || return 0
  file="/ops-state/${name}.json"
  now="$(date +%s)"
  last_ok="$(sed -n 's/.*"last_success": *\([0-9][0-9]*\).*/\1/p' "${file}" 2>/dev/null | head -n 1 || true)"
  [ "${outcome}" = ok ] && last_ok="${now}"
  printf '{"outcome": "%s", "last_attempt": %s, "last_success": %s}\n' \
    "${outcome}" "${now}" "${last_ok:-null}" > "${file}.tmp"
  chmod 644 "${file}.tmp"
  mv "${file}.tmp" "${file}"
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
