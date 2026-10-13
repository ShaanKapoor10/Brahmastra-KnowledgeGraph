#!/usr/bin/env bash
# backup | restore-test | restore [...] | schedule (default)
#
# The schedule is a plain loop rather than cron: cron needs root (or setuid
# helpers) to run jobs, and this container runs unprivileged with every
# capability dropped.
set -Eeuo pipefail

case "${1:-schedule}" in
  backup)       exec /usr/local/bin/backup.sh ;;
  restore-test) exec /usr/local/bin/restore-test.sh ;;
  restore)      shift; exec /usr/local/bin/restore.sh "$@" ;;
  schedule)
    echo "backups daily at ${BACKUP_TIME:-02:30}; restore test on weekday ${RESTORE_TEST_WEEKDAY:-7} at ${RESTORE_TEST_TIME:-04:00} (TZ=${TZ:-UTC})" >&2
    last_backup=""
    last_test=""
    while true; do
      now=$(date +%H:%M)
      today=$(date +%F)
      weekday=$(date +%u)
      if [ "${now}" = "${BACKUP_TIME:-02:30}" ] && [ "${last_backup}" != "${today}" ]; then
        last_backup="${today}"
        /usr/local/bin/backup.sh || true            # failures alert from inside
      fi
      if [ "${weekday}" = "${RESTORE_TEST_WEEKDAY:-7}" ] && [ "${now}" = "${RESTORE_TEST_TIME:-04:00}" ] \
         && [ "${last_test}" != "${today}" ]; then
        last_test="${today}"
        /usr/local/bin/restore-test.sh || true
      fi
      sleep 20
    done
    ;;
  *) exec "$@" ;;
esac
