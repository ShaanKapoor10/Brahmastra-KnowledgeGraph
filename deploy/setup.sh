#!/usr/bin/env bash
# First install of Brahmastra on a Docker host. Safe to run again: it keeps
# existing secrets, an existing admin and running containers, and only fills
# in what is missing.
#
#   ./setup.sh --domain kb.example.com --email ops@example.com [--admin you@example.com]
#   ./setup.sh --domain localhost --tls internal --http-port 8080 --https-port 8443   # a trial
#   ... --admin ops@example.com --admin-password-stdin < password-file       # unattended
#
# What it does:
#   1. preflight: Docker and Compose v2, memory, disk, ports, DNS
#   2. writes deploy/.env.production (mode 600) from the template, generating
#      every empty secret; never prints one
#   3. installs the pinned BRAHMASTRA_VERSION and starts the stack: the signed
#      release images, verified and pulled by digest (RELEASE_SOURCE=registry,
#      the default), or images built here from the tag (RELEASE_SOURCE=source)
#   4. waits until the API is healthy, then creates the first admin if none exists
#   5. runs a first backup, so the install starts with one
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

domain="" email="" admin="" tls="" http_port="" https_port="" skip_dns="" pw_stdin=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="$2"; shift ;;
    --email) email="$2"; shift ;;
    --admin) admin="$2"; shift ;;
    --tls) tls="$2"; shift ;;
    --http-port) http_port="$2"; shift ;;
    --https-port) https_port="$2"; shift ;;
    --skip-dns-check) skip_dns=1 ;;
    --admin-password-stdin) pw_stdin=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

# Read the admin password NOW, before any docker command runs: `docker compose
# exec` and `run` attach stdin and would swallow it. Held only in this
# process's memory and handed to the backend through a pipe.
admin_pw=""
if [ -n "${pw_stdin}" ]; then
  IFS= read -r admin_pw || true
  [ -n "${admin_pw}" ] || die "--admin-password-stdin was given but stdin was empty"
fi
exec </dev/null

info "${BOLD}Brahmastra setup${OFF}"

# ---- 1. preflight --------------------------------------------------------------
require_cmd docker "install Docker Engine: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' plugin)."
docker info >/dev/null 2>&1 || die "cannot talk to Docker: is the daemon running, and is this user in the docker group?"
ok "Docker $(docker version --format '{{.Server.Version}}') with Compose $(docker compose version --short)"

if [ -r /proc/meminfo ]; then
  mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  if [ "${mem_gb}" -lt 3 ]; then
    die "${mem_gb} GB RAM: at least 4 GB is needed (backend ~1.5 GB, Neo4j ~1.5 GB, Postgres, dashboard)."
  elif [ "${mem_gb}" -lt 4 ]; then
    warn "${mem_gb} GB RAM: 4 GB is recommended. Lower NEO4J_MEMORY/BACKEND_MEMORY if containers are killed."
  else
    ok "${mem_gb} GB RAM"
  fi
fi
free_gb=$(df -Pk "${DEPLOY_DIR}" | awk 'NR==2 {printf "%d", $4/1024/1024}')
[ "${free_gb}" -ge 10 ] || die "${free_gb} GB free disk: at least 10 GB is needed for images, data and local backups."
ok "${free_gb} GB free disk"

# ---- 2. configuration -------------------------------------------------------------
if [ ! -f "${ENV_FILE}" ]; then
  cp "${DEPLOY_DIR}/.env.production.example" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  ok "created deploy/.env.production from the template"
else
  chmod 600 "${ENV_FILE}"
  ok "using the existing deploy/.env.production"
fi

[ -n "${domain}" ] && env_set DOMAIN "${domain}" && env_set PUBLIC_URL "https://${domain}"
[ -n "${email}" ] && env_set CADDY_TLS "${email}"
[ -n "${tls}" ] && env_set CADDY_TLS "${tls}"
[ -n "${http_port}" ] && env_set HTTP_PORT "${http_port}"
[ -n "${https_port}" ] && env_set HTTPS_PORT "${https_port}"
[ -n "${https_port}" ] && [ "${https_port}" != 443 ] && \
  env_set PUBLIC_URL "https://$(env_get DOMAIN):${https_port}"

domain="$(env_get DOMAIN)"
case "${domain}" in
  ""|kb.example.com) die "set the domain: ./setup.sh --domain kb.yourcompany.com --email you@yourcompany.com" ;;
esac
case "$(env_get CADDY_TLS)" in
  ""|admin@example.com) die "set an email for Let's Encrypt (--email), or --tls internal for a trial install" ;;
esac

for key in POSTGRES_PASSWORD NEO4J_PASSWORD RESTIC_PASSWORD GRAFANA_ADMIN_PASSWORD; do
  if [ -z "$(env_get "${key}")" ]; then
    env_set "${key}" "$(random_secret)"
    ok "generated ${key}"
  fi
done

if [ "$(env_get BACKUP_OFFSITE)" != "off" ]; then
  repo="$(env_get RESTIC_REPOSITORY)"
  case "${repo}" in
    ""|*your-bucket*) warn "Off-host backups are not configured yet: set RESTIC_REPOSITORY and the AWS_* keys in deploy/.env.production. Until then nightly backups will FAIL and alert, by design." ;;
    *) ok "off-host backups go to ${repo%%\?*}" ;;
  esac
fi
warn "Store RESTIC_PASSWORD from deploy/.env.production in your password manager now. Without it, the encrypted backups cannot be read."

# ---- ports and DNS ---------------------------------------------------------------------
for port in "$(env_get HTTP_PORT)" "$(env_get HTTPS_PORT)"; do
  [ -n "${port}" ] || continue
  if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :${port}" | grep -q . \
     && ! "${COMPOSE[@]}" ps --services --status running 2>/dev/null | grep -qx caddy; then
    die "port ${port} is already in use by another program"
  fi
done
if [ -z "${skip_dns}" ] && [ "$(env_get CADDY_TLS)" != internal ] && command -v getent >/dev/null 2>&1; then
  if ! getent hosts "${domain}" >/dev/null; then
    die "${domain} does not resolve yet. Point an A/AAAA record at this server (or pass --skip-dns-check)."
  fi
  ok "${domain} resolves to $(getent hosts "${domain}" | awk '{print $1}' | head -n 1)"
fi

# ---- 3. build and start ---------------------------------------------------------------
version="$(current_version)"
[ -n "${version}" ] || die "BRAHMASTRA_VERSION is empty in deploy/.env.production"
case "$(release_source)" in
  registry)
    [ -n "$(env_get GHCR_PULL_TOKEN)" ] || warn "GHCR_PULL_TOKEN is empty: pulling private release images will fail"
    if [ -f "${RELEASE_DIR}/last-known-good.env" ] && [ "$(manifest_get "${RELEASE_DIR}/last-known-good.env" VERSION)" = "${version}" ] \
       && all_images_present "${RELEASE_DIR}/last-known-good.env"; then
      ok "release v${version} already installed and verified"
    else
      info "verifying and pulling the signed release ${BOLD}v${version}${OFF}"
      git -C "${REPO_DIR}" fetch --quiet --tags origin || warn "could not fetch tags; using the tags present locally"
      prepare_release "${version}" || die "release v${version} failed verification; nothing was started"
      use_images_from "${RELEASE_DIR}/v${version}/release.env"
    fi
    ;;
  source)
    info "building images for ${BOLD}${version}${OFF} (first build takes several minutes)"
    use_images_from ""
    compose_build --pull --quiet
    ;;
  *) die "RELEASE_SOURCE must be registry or source" ;;
esac
"${COMPOSE[@]}" up -d --remove-orphans
info "waiting for the API to become healthy"
wait_healthy 420 || die "the API did not become healthy. See: docker compose -f deploy/docker-compose.prod.yml logs backend"
ok "stack is up"
if [ "$(release_source)" = registry ] && [ -f "${RELEASE_DIR}/v${version}/release.env" ]; then
  record_known_good "${RELEASE_DIR}/v${version}/release.env"
fi

# ---- 4. first admin -------------------------------------------------------------------
users=$("${COMPOSE[@]}" exec -T backend python -c \
  "from brahmastra.accounts import store; print(store().user_count())" 2>/dev/null | tail -n 1 || echo 0)
if [ "${users:-0}" = "0" ]; then
  if [ -z "${admin}" ]; then
    read -r -p "Email for the first admin: " admin </dev/tty
  fi
  if [ -n "${pw_stdin}" ]; then
    # Unattended installs (Ansible, CI): the password arrives on stdin and is
    # passed straight through, never into argv, the environment or a log.
    printf '%s\n' "${admin_pw}" | "${COMPOSE[@]}" exec -T backend \
      python -m brahmastra.accounts create-admin --email "${admin}" --password-stdin
    admin_pw=""
  else
    info "Choose the admin's password (at least 10 characters; it is not echoed)."
    "${COMPOSE[@]}" exec backend python -m brahmastra.accounts create-admin --email "${admin}" </dev/tty
  fi
else
  ok "accounts already exist (${users}); not creating an admin"
fi

# ---- 5. first backup ----------------------------------------------------------------
if "${COMPOSE[@]}" run --rm -T backup backup; then
  ok "first backup taken"
else
  warn "the first backup failed (usually: off-host storage not configured yet). Fix it, then: docker compose -f deploy/docker-compose.prod.yml run --rm backup backup"
fi

info ""
ok "${BOLD}Brahmastra ${version} is running at $(env_get PUBLIC_URL)${OFF}"
info "Uptime monitor URL: $(env_get PUBLIC_URL)/healthz"
info "Next: harden the host and set up monitoring (docs/DEPLOY_VPS.md)."
