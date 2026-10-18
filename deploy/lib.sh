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

# Build our own images. Compose builds through buildx bake, which can ask for an
# interactive filesystem grant to read a Dockerfile (seen with Compose 5.x on
# Windows even for paths inside the checkout), and an unattended run cannot
# answer. That grant protects against UNTRUSTED build definitions reading host
# files; ours is the compose file of the release tag being installed, so the
# check is relaxed for this one command and nowhere else.
compose_build() {
  BUILDX_BAKE_ENTITLEMENTS_FS=0 "${COMPOSE[@]}" build "$@"
}

if [ -t 1 ]; then BOLD=$'\e[1m'; RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; OFF=$'\e[0m'
else BOLD=""; RED=""; YEL=""; GRN=""; OFF=""; fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "${GRN}" "${OFF}" "$*"; }
warn() { printf '%s! %s%s\n' "${YEL}" "$*" "${OFF}" >&2; }
die()  { printf '%s✗ %s%s\n' "${RED}" "$*" "${OFF}" >&2; exit 1; }

# Read one key from the env file without sourcing it (sourcing would execute it).
# An absent key is an empty value, not an error: under `set -e -o pipefail` a
# grep that matches nothing would otherwise end the calling script silently --
# which is what an optional setting such as NEO4J_DATA_VOLUME did.
env_get() {
  [ -f "${ENV_FILE}" ] || return 0
  { grep -E "^$1=" "${ENV_FILE}" || true; } | tail -n 1 | cut -d= -f2-
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
  # Wait until the backend is FULLY healthy, from inside the network. "degraded"
  # (graph engine down) still serves people, but an upgrade or a graph swap
  # must not count it as done.
  local timeout="${1:-300}" waited=0
  while [ "${waited}" -lt "${timeout}" ]; do
    if "${COMPOSE[@]}" exec -T backend python -c \
      "import json,urllib.request,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8001/health/uptime', timeout=5)).get('status') == 'ok' else 1)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

current_version() { env_get BRAHMASTRA_VERSION; }

# ---- signed releases ---------------------------------------------------------------
# A release is installed from images the release workflow built and signed, not
# from whatever this server builds. Trust is anchored HERE, in the checkout --
# never in the env file or anything downloaded: the repository, the workflow
# file and the tag the signing certificate must name, and the OIDC issuer that
# vouched for it. A fork that publishes its own releases changes RELEASE_REPO.
RELEASE_REPO="ShaanKapoor10/Brahmastra-KnowledgeGraph"
RELEASE_WORKFLOW=".github/workflows/release.yml"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
COSIGN_IMAGE="ghcr.io/sigstore/cosign/cosign:v3.0.6@sha256:de9c65609e6bde17e6b48de485ee788407c9502fa08b8f4459f595b21f56cd00"
RELEASE_DIR="${DEPLOY_DIR}/releases"              # verified manifests + last-known-good
IMAGE_KEYS=(BACKEND_IMAGE FRONTEND_IMAGE BACKUP_IMAGE)

# "registry" (signed images; the default) or "source" (build on this server).
release_source() { local s; s="$(env_get RELEASE_SOURCE)"; printf '%s' "${s:-registry}"; }

release_identity() { printf 'https://github.com/%s/%s@refs/tags/%s' "${RELEASE_REPO}" "${RELEASE_WORKFLOW}" "$1"; }

# A throwaway Docker config holding only the GHCR pull token, so neither cosign
# nor the pulls touch (or leave anything in) the host's own Docker login.
_registry_auth_dir() {
  local dir user token
  dir="$(mktemp -d)"
  token="$(env_get GHCR_PULL_TOKEN)"
  user="$(env_get GHCR_PULL_USER)"
  if [ -n "${token}" ]; then
    printf '{"auths":{"ghcr.io":{"auth":"%s"}}}\n' \
      "$(printf '%s:%s' "${user:-token}" "${token}" | base64 | tr -d '\n')" > "${dir}/config.json"
  else
    printf '{}\n' > "${dir}/config.json"
  fi
  # cosign runs as a non-root user; the parent mktemp directory stays 700.
  chmod 755 "${dir}"; chmod 644 "${dir}/config.json"
  printf '%s' "${dir}"
}

_cosign() {   # auth dir, work dir, cosign args...
  local auth="$1" work="$2"; shift 2
  docker run --rm --read-only --tmpfs /tmp -e HOME=/tmp -e TUF_ROOT=/tmp/sigstore \
    -e DOCKER_CONFIG=/auth -v "${auth}:/auth:ro" -v "${work}:/work:ro" \
    "${COSIGN_IMAGE}" "$@"
}

# Download, verify and validate the release manifest for a version, then
# verify and pull every image it names. On success the manifest is kept at
# ${RELEASE_DIR}/v<version>/release.env. Nothing is changed on failure.
prepare_release() {
  local version="$1" tag="v$1" scratch work auth url line key value owner identity
  owner="$(printf '%s' "${RELEASE_REPO%%/*}" | tr '[:upper:]' '[:lower:]')"
  identity="$(release_identity "${tag}")"
  scratch="$(mktemp -d)"; work="${scratch}/work"; mkdir "${work}"; chmod 755 "${work}"
  auth="$(_registry_auth_dir)"
  # shellcheck disable=SC2064   # expand now: the trap must remove THESE directories
  trap "rm -rf '${scratch}' '${auth}'; trap - RETURN" RETURN

  url="https://github.com/${RELEASE_REPO}/releases/download/${tag}"
  for f in release.env release.env.sigstore.json; do
    curl -fsSL --proto '=https' --tlsv1.2 -m 60 -o "${work}/${f}" "${url}/${f}" \
      || { warn "could not download ${f} for ${tag}"; return 1; }
  done
  chmod 644 "${work}"/*

  # 1. the manifest was signed by OUR release workflow, for THIS tag.
  _cosign "${auth}" "${work}" verify-blob --bundle /work/release.env.sigstore.json \
    --certificate-identity "${identity}" --certificate-oidc-issuer "${OIDC_ISSUER}" \
    /work/release.env >/dev/null 2>&1 \
    || { warn "release manifest signature for ${tag} did NOT verify against ${identity}"; return 1; }

  # 2. it says exactly what a manifest may say, and nothing else. It is parsed,
  #    never sourced: a signed file is still data.
  local -A seen=()
  while IFS= read -r line || [ -n "${line}" ]; do
    [ -n "${line}" ] || continue
    key="${line%%=*}"; value="${line#*=}"
    case "${key}" in
      VERSION)        [ "${value}" = "${version}" ] || { warn "manifest is for ${value}, not ${version}"; return 1; } ;;
      GIT_COMMIT)     [[ "${value}" =~ ^[0-9a-f]{40}$ ]] || { warn "manifest GIT_COMMIT is malformed"; return 1; } ;;
      BACKEND_IMAGE|FRONTEND_IMAGE|BACKUP_IMAGE)
        local svc; svc="$(printf '%s' "${key%_IMAGE}" | tr '[:upper:]' '[:lower:]')"
        [[ "${value}" =~ ^ghcr\.io/${owner}/brahmastra-${svc}@sha256:[0-9a-f]{64}$ ]] \
          || { warn "manifest ${key} is not a ghcr.io/${owner} digest reference"; return 1; } ;;
      *) warn "manifest has an unexpected line (${key})"; return 1 ;;
    esac
    seen[${key}]="${value}"
  done < "${work}/release.env"
  for key in VERSION GIT_COMMIT "${IMAGE_KEYS[@]}"; do
    [ -n "${seen[${key}]:-}" ] || { warn "manifest is missing ${key}"; return 1; }
  done

  # 3. the configuration in this checkout is the one the images were built with.
  local tag_commit
  tag_commit="$(git -C "${REPO_DIR}" rev-parse --verify --quiet "${tag}^{commit}" || true)"
  [ "${tag_commit}" = "${seen[GIT_COMMIT]}" ] \
    || { warn "tag ${tag} is ${tag_commit:-missing} here but the signed manifest says ${seen[GIT_COMMIT]}"; return 1; }

  # 4. every image, by digest, was signed by the same workflow identity -- all
  #    of them checked before any is pulled.
  for key in "${IMAGE_KEYS[@]}"; do
    _cosign "${auth}" "${work}" verify --certificate-identity "${identity}" \
      --certificate-oidc-issuer "${OIDC_ISSUER}" "${seen[${key}]}" >/dev/null 2>&1 \
      || { warn "signature on ${seen[${key}]} did NOT verify"; return 1; }
  done
  for key in "${IMAGE_KEYS[@]}"; do
    docker --config "${auth}" pull --quiet "${seen[${key}]}" >/dev/null \
      || { warn "could not pull ${seen[${key}]} (is GHCR_PULL_TOKEN set and valid?)"; return 1; }
  done

  mkdir -p "${RELEASE_DIR}/${tag}"
  install -m 600 "${work}/release.env" "${RELEASE_DIR}/${tag}/release.env"
  ok "release ${tag} verified: manifest and $(( ${#IMAGE_KEYS[@]} )) images signed by ${RELEASE_REPO} ${RELEASE_WORKFLOW}"
}

all_images_present() {
  local key
  for key in "${IMAGE_KEYS[@]}"; do
    docker image inspect "$(manifest_get "$1" "${key}")" >/dev/null 2>&1 || return 1
  done
}

manifest_get() { { grep -E "^$2=" "$1" || true; } | tail -n 1 | cut -d= -f2-; }

# Point the install at the images a manifest names (or, with no manifest, back
# to images built locally for BRAHMASTRA_VERSION).
use_images_from() {
  local manifest="${1:-}" key
  for key in "${IMAGE_KEYS[@]}"; do
    if [ -n "${manifest}" ]; then env_set "${key}" "$(manifest_get "${manifest}" "${key}")"
    else env_set "${key}" ""; fi
  done
}

# After a release is healthy: it becomes last-known-good, and the one it
# replaced becomes previous-known-good -- what `upgrade.sh --rollback` restores.
record_known_good() {
  local manifest="$1"
  mkdir -p "${RELEASE_DIR}"
  if [ -f "${RELEASE_DIR}/last-known-good.env" ] \
     && [ "$(manifest_get "${RELEASE_DIR}/last-known-good.env" VERSION)" != "$(manifest_get "${manifest}" VERSION)" ]; then
    install -m 600 "${RELEASE_DIR}/last-known-good.env" "${RELEASE_DIR}/previous-known-good.env"
  fi
  { cat "${manifest}"; printf 'VERIFIED_HEALTHY_AT=%s\n' "$(date -u +%FT%TZ)"; } > "${RELEASE_DIR}/last-known-good.env"
  chmod 600 "${RELEASE_DIR}/last-known-good.env"
}
