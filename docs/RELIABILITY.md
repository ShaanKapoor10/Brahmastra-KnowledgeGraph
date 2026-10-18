# Reliability: objectives, budgets and how failure is handled

This is the contract between Brahmastra and the team that runs it. It covers:
- what "working" means, as numbers;
- how much failure is acceptable before priorities change;
- how long each dependency may make a request wait;
- what still works when a dependency is down.

The alerts in `deploy/monitoring/rules/brahmastra.rules.yml` implement these
numbers, and `docs/RUNBOOK.md` says what to do when one fires.

## Service level indicators (SLIs)

| SLI | Definition | Measured at | Excludes |
|---|---|---|---|
| **API availability** | share of API requests that do **not** return 5xx | the backend's request middleware (`main.trace_and_log`), every request | health and metrics probes, `/ask`, pipeline runs, transcript ingest |
| **API latency** | share of API requests answered within **1.5 s** | same point; histogram `brahmastra_http_request_duration_seconds` | same as above |
| **Ask latency** | share of `/ask` answers returned within **10 s** | `rag.answer_question`; histogram `brahmastra_ask_duration_seconds` | none (degraded answers count) |
| **Reachability** (user view) | `/healthz` answers from outside the network | an external uptime monitor, every 60 s | none |

**Window:** a 30-day rolling window for every objective.

**Measurement point:** server-side at the backend. Server-side SLIs see every
request and are precise, but cannot see the network, TLS or Caddy. The
external uptime check covers those, which is why it stays even when the
bundled monitoring is on.

## Service level objectives (SLOs)

| SLO | Objective | Error budget (30 days) |
|---|---|---|
| API availability | **99.5%** of requests succeed | 0.5%, about **3.6 hours** of total outage |
| API latency | **99%** within 1.5 s | 1% of requests may be slower |
| Ask latency | **95%** within 10 s | 5% of answers may be slower |
| Reachability | **99.5%** of external checks succeed | about 3.6 hours |

`/ask` has its own objective because it waits on a language model, whose
latency the team does not control. Folding it into the API objective would
make that objective either meaningless or always breached.

**A degraded answer still counts as available.** When the graph engine is
down, Ask answers from notes (labelled as such), and those answers count as
successes for availability. The graph outage is tracked separately by the
`BrahmastraGraphEngineDown` alert.

## Alerting on the budget

Following the multi-window, multi-burn-rate method (Google SRE Workbook,
"Alerting on SLOs"):

| Alert | Fires when | Means | Severity |
|---|---|---|---|
| ErrorBudgetFastBurn | error ratio > 14.4 × 0.5% over **1 h and 5 m** | 2% of the month's budget gone in an hour | page |
| ErrorBudgetSlowBurn | error ratio > 6 × 0.5% over **6 h and 30 m** | 5% of the budget gone in 6 hours | ticket |
| LatencyBudgetBurn | slow ratio > 14.4 × 1% (1 h) or 6 × 1% (6 h) | latency budget burning | ticket |
| AskLatencyBurn | > 25% of answers slower than 10 s for an hour | Ask far outside its objective | ticket |

The short window stops an alert firing on an incident that is already over.
The long window stops a brief blip from paging anyone. The burn rules are
unit-tested (`deploy/monitoring/tests/alerts.test.yml`), including that failing
health probes do **not** count.

## Error-budget policy

Agreed in advance, so the decision is not made in the middle of an incident.

1. **Budget remaining (> 0):** release normally. Reliability work competes
   with features on merit.
2. **Budget below 25% with more than a week of the window left:** releases
   need a named rollback owner, and the next sprint includes the top
   reliability fix from the most recent postmortem.
3. **Budget exhausted:** **feature releases stop** until the 30-day error ratio
   is back within the objective. Only reliability fixes, security fixes and
   rollbacks ship. The freeze lifts by itself when the SLI recovers. It can be
   overridden only by the service owner, in writing, with the reason recorded
   in the postmortem.
4. **Any single incident that spends more than 20% of the budget** gets a
   blameless postmortem (`docs/POSTMORTEM_TEMPLATE.md`) within five working
   days, with action items in the tracker.
5. **Unreliability caused by a dependency** (an LLM provider, Notion) still
   counts against the budget. The team owns the experience, including
   choosing fallbacks.

Review the objectives quarterly. A target that is never close to its budget
is too loose; one that is always exhausted is wrong or underfunded.

## Timeout and retry budgets, per dependency

Every outbound call has a ceiling. A dependency that does not answer must cost
a bounded wait, never a hung request.

| Dependency | Connect | Per call | Retries | Circuit breaker / fallback | Setting |
|---|---|---|---|---|---|
| **Postgres** (system of record) | 10 s | statement timeout 60 s | none (a failure is reported; requests fail fast) | none: without it nothing can be served, so readiness is 503 | `POSTGRES_CONNECT_TIMEOUT`, `POSTGRES_STATEMENT_TIMEOUT_MS` |
| **Neo4j, local** (compose) | 5 s; pool acquire 5 s | query timeout 120 s | driver-managed transaction retry up to 10 s | opens after 3 failures in 30 s, then fails instantly for 30 s; graph routes serve the snapshot, Ask uses notes | `NEO4J_CONNECT_TIMEOUT`, `NEO4J_ACQUIRE_TIMEOUT`, `NEO4J_QUERY_TIMEOUT`, `NEO4J_RETRY_TIME`, `NEO4J_BREAKER_*` |
| **Neo4j, Aura** | 20 s; acquire 45 s | 120 s | up to 30 s | same breaker; longer waits because Aura is slow to wake | same |
| **LLM providers** | provider SDK | 240 s per call | 3 attempts, waiting as long as the provider says (capped at 45 s). Groq: other keys tried first, then at most 2 waits | a spent quota moves to the next usable provider; a retired model fails at once | `LLM_MAX_BACKOFF`, `GROQ_API_KEYS` |
| **Notion** | SDK | 30 s | none per call; the next scheduler tick retries | sync and write-back are skipped stages, never failures of the run | `NOTION_TIMEOUT_MS` |
| **Dashboard → API** | | 30 s; 180 s for Ask, pipeline and ingest | none | the page shows what loaded, plus a banner or an "unreachable" screen | `frontend/lib/backend.ts` |
| **Caddy → app** | Caddy defaults | Caddy defaults | none | `/healthz` reports up, degraded or down | `deploy/Caddyfile` |

**Retry rules:**
- Retry only what can succeed on retry (rate limits, timeouts, connection
  resets), never a bad request.
- Honour the server's stated delay; a blind backoff spends retries inside the
  window the server said was closed.
- Bound the total: three attempts, at most 45 s per wait.
- Never retry a quota refusal.

## What still works when something is down

| Down | Still works | Shows | Readiness |
|---|---|---|---|
| **Neo4j** | notes, note search, Ask (from notes, labelled), ingest, sign-in | the last saved graph, marked stale, with its timestamp in a banner | 200 `degraded` |
| **Postgres** | nothing that needs data | "Brahmastra can't reach its server" | 503 |
| **LLM provider** | everything except extraction, Ask and meeting comprehension | errors on those actions; pending notes retry on the next run | 200 |
| **Notion** | everything | the sync stage reports skipped or errored | 200 |
| **Monitoring stack** | everything | the external uptime check still pages | 200 |

### Acceptance test: stopping the graph engine

Run against a staging install after any change to degradation behaviour (last
run: see the Phase 3 commit):

```bash
bctl stop neo4j
curl -s https://DOMAIN/healthz                 # {"status":"degraded"}, HTTP 200
# Dashboard: the banner reads "The knowledge graph is temporarily unavailable.
#   You are seeing the graph as it was on <time>". Notes list and search work.
# Ask: the answer carries "graph unavailable, answered from your notes".
bctl start neo4j                                # within ~30 s everything is back to normal
```

Automated equivalents run in the test suite (`tests/test_degradation.py`,
`tests/test_health.py`).
