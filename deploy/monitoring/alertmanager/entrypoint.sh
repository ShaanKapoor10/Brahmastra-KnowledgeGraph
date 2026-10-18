#!/bin/sh
# Assemble Alertmanager's config for this install and start it.
#
#   ALERT_RECEIVER     slack | msteams | webhook   where alerts go
#   ALERT_WEBHOOK_URL  that receiver's URL (Slack/Teams incoming webhook, or any endpoint)
#   WATCHDOG_URL       optional dead-man's switch for the always-firing Watchdog alert
#
# URLs are secrets (anyone holding one can post into your channel), so they are
# written to files under /tmp (tmpfs, mode 600) and referenced with *_file
# options -- never written into the config, and never printed.
#
# No receiver URL is a REFUSAL, not a warning: monitoring that alerts nobody
# looks healthy while being useless, which is worse than no monitoring at all.
set -eu
umask 077
kind="${ALERT_RECEIVER:-slack}"
case "${kind}" in
  slack|msteams|webhook) ;;
  *) echo "alertmanager: ALERT_RECEIVER must be slack, msteams or webhook (got '${kind}')" >&2; exit 1 ;;
esac
if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
  echo "alertmanager: ALERT_WEBHOOK_URL is empty: alerts would reach nobody. Set it in deploy/.env.production." >&2
  exit 1
fi
mkdir -p /tmp/am
printf '%s' "${ALERT_WEBHOOK_URL}" > /tmp/am/receiver_url
printf '%s' "${WATCHDOG_URL:-http://127.0.0.1:9/unused}" > /tmp/am/watchdog_url
cat /etc/alertmanager/base.yml "/etc/alertmanager/receivers/${kind}.yml" > /tmp/am/alertmanager.yml
if [ -z "${WATCHDOG_URL:-}" ]; then
  # No dead-man's switch configured: route the Watchdog to nowhere instead.
  sed -i 's/      receiver: watchdog/      receiver: "null"/' /tmp/am/alertmanager.yml
fi
echo "alertmanager: sending alerts to the ${kind} receiver" >&2
exec /bin/alertmanager --config.file=/tmp/am/alertmanager.yml --storage.path=/alertmanager \
  --web.external-url="${ALERTMANAGER_EXTERNAL_URL:-http://localhost:9093}"
