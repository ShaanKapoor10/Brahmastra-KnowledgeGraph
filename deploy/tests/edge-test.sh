#!/usr/bin/env bash
# Test the public edge (deploy/Caddyfile) against stub upstreams.
#
# Runs the real Caddyfile in the pinned Caddy image, in front of two stub
# servers named `frontend` and `backend` that answer every path with
# "upstream:<name>:<path>". It asserts what reaches an upstream and what the
# edge refuses first -- above all that /metrics never leaves the internal
# network. Needs only Docker; run it in CI.
#
#   deploy/tests/edge-test.sh
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

net="edge-test-$$"
cleanup() { docker rm -f "${net}-caddy" "${net}-backend" "${net}-frontend" >/dev/null 2>&1 || true
            docker network rm "${net}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create "${net}" >/dev/null

stub='import http.server,os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body=("upstream:"+os.environ["NAME"]+":"+self.path).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass
http.server.HTTPServer(("0.0.0.0",int(os.environ["PORT"])),H).serve_forever()'
docker run -d --name "${net}-backend" --network "${net}" --network-alias backend \
  -e NAME=backend -e PORT=8001 python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 python -c "${stub}" >/dev/null
docker run -d --name "${net}-frontend" --network "${net}" --network-alias frontend \
  -e NAME=frontend -e PORT=3000 python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 python -c "${stub}" >/dev/null
docker run -d --name "${net}-caddy" --network "${net}" -e DOMAIN=localhost -e CADDY_TLS=internal \
  -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.6-alpine@sha256:d44355d3c2149dc580ce2cac735955d1c08d3d00882c30489c241aa51a5c10d9 >/dev/null
sleep 3

fail=0
check() {   # path, expected status, expected body prefix ("" = not proxied)
  local path="$1" want_status="$2" want_body="$3" got status body
  got="$(docker run --rm --network "${net}" curlimages/curl:8.10.1@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b -sk -w '\n%{http_code}' \
         --resolve "localhost:443:$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${net}-caddy")" \
         "https://localhost${path}")"
  status="${got##*$'\n'}"; body="${got%$'\n'*}"
  if [ "${status}" = "${want_status}" ] && { [ -z "${want_body}" ] && [[ "${body}" != upstream:* ]] || [[ "${body}" == "${want_body}"* ]]; }; then
    printf 'ok    %-22s %s %s\n' "${path}" "${status}" "${body:0:40}"
  else
    printf 'FAIL  %-22s got %s %s (want %s %s)\n' "${path}" "${status}" "${body:0:40}" "${want_status}" "${want_body:-<not proxied>}"
    fail=1
  fi
}

check /metrics            404 ""
check /metrics/anything   404 ""
check /v1/metrics         404 ""
check /v1/health/ready    404 ""
check /v1/health          404 ""
check /healthz            200 "upstream:backend:/health/uptime"
check /v1/notes           200 "upstream:backend:/notes"
check /                   200 "upstream:frontend:/"
check /login              200 "upstream:frontend:/login"
exit "${fail}"
