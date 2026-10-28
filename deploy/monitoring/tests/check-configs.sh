#!/usr/bin/env bash
# Validate the monitoring bundle with the tools of the PINNED images, exactly as
# they will load it: Prometheus config + rules (promtool check), the alert unit
# tests (promtool test rules), and Alertmanager's config as entrypoint.sh
# assembles it for EACH receiver (amtool check-config). Needs only Docker.
#
#   deploy/monitoring/tests/check-configs.sh
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mon="$(pwd)"
PROM="prom/prometheus:v2.55.1@sha256:2659f4c2ebb718e7695cb9b25ffa7d6be64db013daba13e05c875451cf51b0d3"
AM="prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a"
export MSYS_NO_PATHCONV=1      # Git Bash on Windows would rewrite the container paths

promtool() {
  docker run --rm --entrypoint promtool \
    -v "${mon}/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
    -v "${mon}/rules:/etc/prometheus/rules:ro" -v "${mon}/tests:/tests:ro" \
    -w /etc/prometheus "${PROM}" "$@"
}

promtool check config --syntax-only /etc/prometheus/prometheus.yml
rules=()
for f in "${mon}"/rules/*.yml; do rules+=("/etc/prometheus/rules/$(basename "${f}")"); done
promtool check rules "${rules[@]}"
# The test file names rule files relative to itself.
docker run --rm --entrypoint sh -v "${mon}/rules:/rules:ro" -v "${mon}/tests:/tests:ro" "${PROM}" -c \
  'mkdir -p /tmp/t && cp /tests/*.yml /tmp/t/ && cp -r /rules /tmp/rules && cd /tmp/t && promtool test rules alerts.test.yml'

for receiver in "${mon}"/alertmanager/receivers/*.yml; do
  kind="$(basename "${receiver}" .yml)"
  docker run --rm --entrypoint sh -v "${mon}/alertmanager:/etc/alertmanager:ro" "${AM}" -c "
    set -e; mkdir -p /tmp/am
    printf https://hooks.example.invalid/x > /tmp/am/receiver_url
    printf https://watchdog.example.invalid/x > /tmp/am/watchdog_url
    cat /etc/alertmanager/base.yml /etc/alertmanager/receivers/${kind}.yml > /tmp/am/alertmanager.yml
    amtool check-config /tmp/am/alertmanager.yml >/dev/null" \
    && echo "alertmanager config with the ${kind} receiver: ok"
done
