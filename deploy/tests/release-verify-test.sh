#!/usr/bin/env bash
# Unit test for the release gate in deploy/lib.sh (prepare_release).
#
# The network, cosign, git and docker are stubbed, so this tests OUR decisions:
# which manifests are refused before anything is pulled, and that a signature
# failure stops everything. cosign's own verification is cosign's to test; the
# release workflow's verify job exercises it for real on every release.
#
#   deploy/tests/release-verify-test.sh
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "${here}/../lib.sh"
set +e                       # each case is judged by its exit status, not by -e

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
RELEASE_DIR="${tmp}/releases"
ENV_FILE="${tmp}/env"; : > "${ENV_FILE}"
COMMIT="0123456789abcdef0123456789abcdef01234567"
D="$(printf 'a%.0s' {1..64})"
OWNER="$(printf '%s' "${RELEASE_REPO%%/*}" | tr '[:upper:]' '[:lower:]')"

# ---- stubs ----------------------------------------------------------------------------
COSIGN_BLOB_RC=0 COSIGN_IMAGE_RC=0 PULLED=""
curl() {                                   # curl ... -o FILE URL
  local out="" a
  while [ $# -gt 0 ]; do a="$1"; shift; [ "${a}" = -o ] && { out="$1"; shift; }; done
  case "${out}" in
    *.sigstore.json) printf '{}' > "${out}" ;;
    *) cp "${tmp}/manifest" "${out}" ;;
  esac
}
COSIGN_FAIL_ON=""
_cosign() {
  shift 2
  [ "$1" = verify-blob ] && return "${COSIGN_BLOB_RC}"
  case "${*: -1}" in *"${COSIGN_FAIL_ON:-@@none@@}"*) return 1 ;; esac
  return "${COSIGN_IMAGE_RC}"
}
git() { printf '%s\n' "${COMMIT}"; }
docker() { [ "$3" = pull ] && PULLED="${PULLED} $5"; return 0; }
chmod() { :; }                             # mktemp dirs on some hosts reject 755 on mounts

manifest() {
  cat > "${tmp}/manifest" <<EOF
VERSION=1.2.3
GIT_COMMIT=${COMMIT}
BACKEND_IMAGE=ghcr.io/${OWNER}/brahmastra-backend@sha256:${D}
FRONTEND_IMAGE=ghcr.io/${OWNER}/brahmastra-frontend@sha256:${D}
BACKUP_IMAGE=ghcr.io/${OWNER}/brahmastra-backup@sha256:${D}
EOF
}

fail=0
expect() {   # want(pass|refuse), description
  local want="$1" what="$2" rc
  PULLED=""
  prepare_release 1.2.3 >/dev/null 2>&1; rc=$?
  if { [ "${want}" = pass ] && [ "${rc}" -eq 0 ]; } || { [ "${want}" = refuse ] && [ "${rc}" -ne 0 ] && [ -z "${PULLED}" ]; }; then
    printf 'ok    %s\n' "${what}"
  else
    printf 'FAIL  %s (rc=%s, pulled:%s)\n' "${what}" "${rc}" "${PULLED:- nothing}"; fail=1
  fi
  COSIGN_BLOB_RC=0 COSIGN_IMAGE_RC=0 COSIGN_FAIL_ON=""
}

manifest;                                              expect pass   "a well-formed, signed manifest is accepted"
[ -f "${RELEASE_DIR}/v1.2.3/release.env" ] && printf 'ok    the verified manifest is kept\n' \
  || { printf 'FAIL  the verified manifest was not kept\n'; fail=1; }
manifest; COSIGN_BLOB_RC=1;                            expect refuse "a manifest whose signature fails is refused"
manifest; COSIGN_IMAGE_RC=1;                           expect refuse "an image whose signature fails is refused, before any pull"
manifest; COSIGN_FAIL_ON=brahmastra-backup;             expect refuse "the LAST image failing still means nothing was pulled"
manifest; sed -i 's/^VERSION=.*/VERSION=1.2.4/' "${tmp}/manifest";            expect refuse "a manifest for another version is refused"
manifest; sed -i "s/^GIT_COMMIT=.*/GIT_COMMIT=$(printf 'f%.0s' {1..40})/" "${tmp}/manifest"; expect refuse "a tag that is not the signed commit is refused"
manifest; sed -i "s#ghcr.io/${OWNER}/brahmastra-backend#ghcr.io/someone-else/brahmastra-backend#" "${tmp}/manifest"; expect refuse "an image from another owner is refused"
manifest; sed -i 's#brahmastra-frontend@sha256#brahmastra-backend@sha256#' "${tmp}/manifest"; expect refuse "an image under the wrong key is refused"
manifest; sed -i 's#@sha256:a*#:latest#' "${tmp}/manifest";                  expect refuse "a tag instead of a digest is refused"
manifest; echo 'LD_PRELOAD=/tmp/x.so' >> "${tmp}/manifest";                   expect refuse "an unexpected key is refused"
manifest; sed -i '/^BACKUP_IMAGE=/d' "${tmp}/manifest";                       expect refuse "a manifest missing an image is refused"
manifest; echo 'BACKEND_IMAGE=$(touch /tmp/pwned)' >> "${tmp}/manifest";      expect refuse "shell syntax in a value is refused, never executed"

exit "${fail}"
