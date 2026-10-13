#!/usr/bin/env bash
# Shared by setup.sh and upgrade.sh. Never prints a secret: values are written
# to the env file and only ever referred to by name.
# Variables defined here (REPO_DIR, colours) are used by the scripts that source it.
# shellcheck disable=SC2034
set -Eeuo pipefail
umask 077

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
ENV_FILE="${DEPLOY_DIR}/.env.production"
COMPOSE=(docker compose --project-directory "${DEPLOY_DIR}" -f "${DEPLOY_DIR}/docker-compose.prod.yml" --env-file "${ENV_FILE}")

# Work from the repository root. The build contexts (../backend, ../frontend)
# live there, and BuildKit refuses to read a context outside the current
# directory without an interactive grant -- which an unattended upgrade cannot
# give. Running from the root keeps that protection on instead of disabling it.
cd "${REPO_DIR}"

if [ -t 1 ]; then BOLD=$'\e[1m'; RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; OFF=$'\e[0m'
else BOLD=""; RED=""; YEL=""; GRN=""; OFF=""; fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "${GRN}" "${OFF}" "$*"; }
warn() { printf '%s! %s%s\n' "${YEL}" "$*" "${OFF}" >&2; }
die()  { printf '%s✗ %s%s\n' "${RED}" "$*" "${OFF}" >&2; exit 1; }

# Read one key from the env file without sourcing it (sourcing would execute it).
env_get() {
  [ -f "${ENV_FILE}" ] || return 0
  grep -E "^$1=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2-
}

# Set one key in the env file, in place. The value never reaches the terminal.
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    awk -v k="${key}" -v v="${value}" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' \
      "${ENV_FILE}" > "${tmp}"
  else
    cat "${ENV_FILE}" > "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi
  chmod 600 "${tmp}"
  mv "${tmp}" "${ENV_FILE}"
}

# 40 characters from [A-Za-z0-9]: no quoting, URL or shell escaping problems.
random_secret() {
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "$1 is required: $2"; }

wait_healthy() {
  # Wait until the backend answers its uptime check from inside the network.
  local timeout="${1:-300}" waited=0
  while [ "${waited}" -lt "${timeout}" ]; do
    if "${COMPOSE[@]}" exec -T backend python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health/uptime', timeout=5).status == 200 else 1)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

current_version() { env_get BRAHMASTRA_VERSION; }
