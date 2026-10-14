#!/usr/bin/env bash
# Back up the system of record: Postgres (notes, workspaces, transcripts,
# accounts). The graph in Neo4j is NOT backed up on purpose -- it is derived
# from the notes and rebuilt by a full pipeline run; see docs/OPERATIONS.md.
#
#   1. pg_dump (custom format, compressed) to /backups, written as .partial then renamed
#   2. verified: pg_restore --list must read it back
#   3. encrypted off-host copy with restic (client-side AES-256), then retention
#   4. local retention: dumps older than BACKUP_KEEP_LOCAL_DAYS removed
#   5. heartbeat on success; alert + failing heartbeat on any error
export JOB=backup
# shellcheck source=common.sh
source /usr/local/bin/common.sh

step="starting"
on_error() {
  alert "Backup FAILED at step '${step}'. Earlier backups are untouched. Check: docker compose logs backup"
  heartbeat "${BACKUP_HEARTBEAT_URL:-}" fail
  record_state backup failed
  rm -f /backups/*.partial 2>/dev/null || true
  exit 1
}
trap on_error ERR

step="configuration"
require_offsite_config
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dump="/backups/brahmastra-${stamp}.dump"

step="pg_dump"
log "dumping ${PGDATABASE} from ${PGHOST}"
pg_dump --format=custom --compress=6 --no-owner --file="${dump}.partial"
mv "${dump}.partial" "${dump}"

step="verify"
entries=$(pg_restore --list "${dump}" | grep -c ' TABLE DATA ' || true)
if [ "${entries}" -eq 0 ]; then
  log "the dump holds no table data"
  false
fi
size=$(du -h "${dump}" | cut -f1)
log "dump ok: $(basename "${dump}") (${size}, ${entries} tables with data)"

if offsite_enabled; then
  step="restic init"
  if ! restic cat config >/dev/null 2>&1; then
    log "initialising the off-host repository"
    restic init >/dev/null
  fi
  step="restic backup"
  restic backup --quiet --tag postgres --host "${INSTANCE_NAME:-brahmastra}" "${dump}"
  step="restic retention"
  restic forget --quiet --tag postgres --keep-daily "${BACKUP_KEEP_DAILY:-7}" \
    --keep-weekly "${BACKUP_KEEP_WEEKLY:-5}" --keep-monthly "${BACKUP_KEEP_MONTHLY:-12}" --prune
  log "off-host copy ok"
fi

step="local retention"
find /backups -maxdepth 1 -name 'brahmastra-*.dump' -mtime "+${BACKUP_KEEP_LOCAL_DAYS:-7}" -delete

trap - ERR
heartbeat "${BACKUP_HEARTBEAT_URL:-}" ok
record_state backup ok
log "backup complete"
