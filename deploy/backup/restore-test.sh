#!/usr/bin/env bash
# Prove the latest backup can actually be restored. A backup nobody has
# restored is a hope, not a backup.
#
# Takes the newest dump -- from the off-host copy when configured, so the test
# covers the copy that would survive losing this machine -- restores it into a
# THROWAWAY Postgres started inside this container, and checks the restored
# data against the live database. The live database is only read.
export JOB=restore-test
# shellcheck source=common.sh
source /usr/local/bin/common.sh

work=/scratch/restore-test
step="starting"
cleanup() {
  pg_ctl -D "${work}/data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "${work}"
}
on_error() {
  alert "Weekly RESTORE TEST FAILED at step '${step}'. Backups may not be restorable; investigate now."
  heartbeat "${RESTORE_TEST_HEARTBEAT_URL:-}" fail
  cleanup
  exit 1
}
trap on_error ERR
trap cleanup EXIT
rm -rf "${work}"
mkdir -p "${work}"

step="fetch dump"
if offsite_enabled; then
  restic restore latest --quiet --tag postgres --target "${work}/fetched"
  dump=$(find "${work}/fetched" -name 'brahmastra-*.dump' | sort | tail -n 1)
  source_label="off-host copy"
else
  dump=$(find /backups -maxdepth 1 -name 'brahmastra-*.dump' | sort | tail -n 1)
  source_label="local dump"
fi
if [ -z "${dump:-}" ] || [ ! -f "${dump}" ]; then
  log "no dump found to test"
  false
fi
log "testing the ${source_label}: $(basename "${dump}")"

step="start throwaway server"
initdb -D "${work}/data" -U postgres --auth=trust >/dev/null
pg_ctl -D "${work}/data" -o "-k ${work} -c listen_addresses= -c fsync=off" -w -l "${work}/pg.log" start >/dev/null

step="restore"
started=$(date +%s)
createdb -h "${work}" -U postgres restored
pg_restore -h "${work}" -U postgres -d restored --no-owner --exit-on-error "${dump}"
took=$(( $(date +%s) - started ))

step="verify"
count() { psql -h "$1" -U "$2" -d "$3" -tAc "SELECT count(*) FROM $4" 2>/dev/null || echo 0; }
restored_notes=$(count "${work}" postgres restored notes)
restored_workspaces=$(count "${work}" postgres restored workspaces)
live_notes=$(count "${PGHOST}" "${PGUSER}" "${PGDATABASE}" notes)
log "restored in ${took}s: ${restored_notes} notes, ${restored_workspaces} workspaces (live now: ${live_notes} notes)"
if [ "${live_notes}" -gt 0 ] && [ "${restored_notes}" -eq 0 ]; then
  log "the live database has notes but the restored copy has none"
  false
fi
# The backup is older than the live data, so it may hold fewer rows -- but a
# large shortfall means backups have not been capturing what is written.
if [ "${live_notes}" -gt 20 ] && [ $(( restored_notes * 100 / live_notes )) -lt 80 ]; then
  log "the restored copy holds under 80% of the live notes"
  false
fi

trap - ERR
heartbeat "${RESTORE_TEST_HEARTBEAT_URL:-}" ok
log "restore test passed"
