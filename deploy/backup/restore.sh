#!/usr/bin/env bash
# DISASTER RECOVERY: replace the live database with a backup.
#
#   docker compose stop backend frontend scheduler
#   docker compose run --rm backup restore --list                # what exists
#   docker compose run --rm backup restore --from latest --yes   # or a dump file / snapshot id
#   docker compose up -d
#   then rebuild the graph from the restored notes (docs/OPERATIONS.md)
#
# Refuses without --yes: it drops and recreates every table in the live database.
export JOB=restore
# shellcheck source=common.sh
source /usr/local/bin/common.sh

from=""
yes=""
list=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) list=1 ;;
    --from) from="${2:-}"; shift ;;
    --yes) yes=1 ;;
    *) log "unknown option $1"; exit 2 ;;
  esac
  shift
done

if [ -n "${list}" ]; then
  log "local dumps:"
  find /backups -maxdepth 1 -name 'brahmastra-*.dump' -printf '%f\n' | sort -r
  if offsite_enabled; then
    log "off-host snapshots:"
    restic snapshots --tag postgres
  fi
  exit 0
fi
if [ -z "${from}" ]; then
  log "say which backup: --from latest, a dump filename, or a restic snapshot id"
  exit 2
fi
if [ -z "${yes}" ]; then
  log "this REPLACES the live database; rerun with --yes"
  exit 2
fi

work=/scratch/restore
rm -rf "${work}"
mkdir -p "${work}"
cleanup() { rm -rf "${work}"; }
trap cleanup EXIT

if [ -f "/backups/${from}" ]; then
  dump="/backups/${from}"
elif [ "${from}" = latest ] && ! offsite_enabled; then
  dump=$(find /backups -maxdepth 1 -name 'brahmastra-*.dump' | sort | tail -n 1)
else
  restic restore "${from}" --quiet --tag postgres --target "${work}"
  dump=$(find "${work}" -name 'brahmastra-*.dump' | sort | tail -n 1)
fi
if [ ! -f "${dump:-}" ]; then
  log "backup not found: ${from}"
  exit 1
fi

log "restoring $(basename "${dump}") into ${PGDATABASE} on ${PGHOST}"
pg_restore --clean --if-exists --no-owner --exit-on-error -d "${PGDATABASE}" "${dump}"
log "restore complete. Start the stack, then rebuild the graph from the notes."
