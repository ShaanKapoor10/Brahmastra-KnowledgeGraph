#!/usr/bin/env bash
# Rebuild the knowledge graph from Postgres into a FRESH Neo4j, verify it, swap.
#
#   ./rebuild-graph.sh                        rebuild, compare with the live graph, swap if identical
#   ./rebuild-graph.sh --accept-differences   swap even if the rebuild differs (after a restore,
#                                             an ontology change, or a deliberate re-extraction)
#   ./rebuild-graph.sh --rollback             point back at the previous graph
#
# Blue/green on two volumes. The live graph keeps serving from one while the
# rebuild fills an empty Neo4j on the other ("neo4j-shadow", profile `rebuild`).
# Only a rebuild that finished in every workspace is swapped in, and the
# previous graph stays on its volume until the next rebuild, for --rollback.
#
# The rebuild runs with Notion and the checkpoint queue switched off: it must
# only READ the system of record, never write to it or to anything outside.
# Model replies come from the reply cache in Postgres, so a repeat rebuild costs
# little; a cache miss calls the configured LLM.
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

accept="" rollback=""
case "${1:-}" in
  --accept-differences) accept=1 ;;
  --rollback) rollback=1 ;;
  "") ;;
  *) die "usage: ./rebuild-graph.sh [--accept-differences | --rollback]" ;;
esac
[ -f "${ENV_FILE}" ] || die "no deploy/.env.production: run ./setup.sh first"

LOCK="${DEPLOY_DIR}/.rebuild.lock"
mkdir "${LOCK}" 2>/dev/null || die "another rebuild is running (or crashed: remove ${LOCK} if you are sure)"
work="$(mktemp -d)"
cleanup() { rmdir "${LOCK}" 2>/dev/null || true; rm -rf "${work}"; }
trap cleanup EXIT

PROJECT="$("${COMPOSE[@]}" config --format json 2>/dev/null | sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' | head -n 1)"
PROJECT="${PROJECT:-brahmastra}"
active="$(env_get NEO4J_DATA_VOLUME)"; active="${active:-neo4j-data}"
if [ "${active}" = neo4j-data ]; then inactive=neo4j-data-b; else inactive=neo4j-data; fi
SHADOW=(env NEO4J_SHADOW_VOLUME="${inactive}" "${COMPOSE[@]}" --profile rebuild)

swap_to() {
  env_set NEO4J_DATA_VOLUME "$1"
  "${COMPOSE[@]}" up -d --wait neo4j
  "${COMPOSE[@]}" restart backend
  "${COMPOSE[@]}" ps --services --status running | grep -qx scheduler && "${COMPOSE[@]}" restart scheduler
  wait_healthy 300
}

if [ -n "${rollback}" ]; then
  info "pointing the graph back at ${inactive} (the previous generation)"
  swap_to "${inactive}" || die "the previous graph did not come up healthy"
  ok "rolled back: the graph is served from ${inactive}"
  exit 0
fi

info "${BOLD}live graph on ${active}; rebuilding into ${inactive}${OFF}"

# ---- 1. fingerprint what is live -------------------------------------------------------
"${COMPOSE[@]}" exec -T backend python -m brahmastra.rebuild --fingerprint > "${work}/live.json" \
  || die "could not fingerprint the live graph"
ok "fingerprinted the live graph"

# ---- 2. an EMPTY shadow Neo4j on the inactive volume ------------------------------------
"${SHADOW[@]}" rm -sf neo4j-shadow >/dev/null 2>&1 || true
docker volume rm -f "${PROJECT}_${inactive}" >/dev/null
"${SHADOW[@]}" up -d --wait neo4j-shadow || die "the shadow Neo4j did not start"
ok "empty shadow Neo4j is up"

# ---- 3. rebuild every workspace into it, comparing as it goes ---------------------------
info "rebuilding (reads Postgres only; Notion and the checkpoint queue are off)"
set +e
"${COMPOSE[@]}" run --rm -T --no-deps \
  -e NEO4J_URI=neo4j://neo4j-shadow:7687 -e NOTION_TOKEN= -e NOTION_DATABASE_ID= \
  -e BRAHMASTRA_CHECKPOINT_DIR=/tmp/no-checkpoints \
  backend python -m brahmastra.rebuild --rebuild-all --expect - \
  < "${work}/live.json" > "${work}/rebuilt.json"
rc=$?
set -e
grep -E '"(status|seconds)"|differences|/' "${work}/rebuilt.json" | sed 's/^ */  /' | head -n 40 || true
if [ "${rc}" -eq 1 ]; then
  "${SHADOW[@]}" stop neo4j-shadow >/dev/null
  die "the rebuild failed in at least one workspace; the live graph is untouched"
fi
if [ "${rc}" -eq 3 ] && [ -z "${accept}" ]; then
  "${SHADOW[@]}" stop neo4j-shadow >/dev/null
  die "the rebuild differs from the live graph (above). Investigate, or rerun with --accept-differences. The live graph is untouched."
fi
[ "${rc}" -eq 0 ] && ok "the rebuild reproduces the live graph exactly" || warn "accepting the differences above"

# ---- 4. swap -----------------------------------------------------------------------------
"${SHADOW[@]}" stop neo4j-shadow >/dev/null
"${SHADOW[@]}" rm -f neo4j-shadow >/dev/null
if ! swap_to "${inactive}"; then
  warn "the rebuilt graph did not come up healthy; switching back to ${active}"
  swap_to "${active}" || die "the previous graph is not healthy either. See docs/OPERATIONS.md."
  die "swap failed and was reverted; the previous graph is serving"
fi

# ---- 5. the graph now serving is the one that was built ----------------------------------
if ! "${COMPOSE[@]}" exec -T backend python -m brahmastra.rebuild --fingerprint --expect - \
     < "${work}/rebuilt.json" >/dev/null; then
  warn "the serving graph does not match what was built; switching back to ${active}"
  swap_to "${active}"
  die "verification failed and the swap was reverted"
fi
printf '%s %s rebuilt-graph %s -> %s\n' "$(date -u +%FT%TZ)" "$(whoami)" "${active}" "${inactive}" \
  >> "${DEPLOY_DIR}/upgrade-history.log"
ok "${BOLD}the rebuilt graph is live on ${inactive}${OFF}; the previous one stays on ${active} for --rollback"
