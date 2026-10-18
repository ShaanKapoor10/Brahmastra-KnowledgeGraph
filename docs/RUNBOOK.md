# Runbook

One section per alert. Every alert in `deploy/monitoring/rules/brahmastra.rules.yml`
links here by anchor, so **keep the headings stable**.

All commands run from the repository root on the server:

```bash
alias bctl='docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production'
```

**First minute, for any alert:**
1. Open the Grafana overview over an SSH tunnel (docs/OBSERVABILITY.md).
2. Run `bctl ps`.
3. Run `bctl logs --since 15m backend | tail -100`.

Find a request across services by its `trace_id`.

---

## api-down
**`BrahmastraDown`: Prometheus cannot scrape the backend.** Everyone is affected.

1. Check the container: `bctl ps backend`.
   - **Restarting:** run `bctl logs --tail 100 backend`.
     - `Refusing to start: unsafe production configuration`: fix
       `deploy/.env.production` (the message lists each problem), then
       `bctl up -d backend`.
     - Out of memory (`OOMKilled` in `docker inspect`): raise
       `BACKEND_MEMORY`, or lower `WEB_CONCURRENCY`.
   - **Running but unhealthy:** check that Postgres is healthy
     (`bctl ps postgres`). Readiness is 503 only when Postgres is down.
2. **Right after an upgrade:** run `cd deploy && ./upgrade.sh --rollback`.
3. Confirm recovery from outside: `curl -s https://DOMAIN/healthz`.

## graph-engine-down
**`BrahmastraGraphEngineDown`: Neo4j has not answered for 5 minutes.**
The product is degraded, not down: notes, search and Ask (answered from notes)
still work, and the dashboard shows the last saved graph with a banner.

1. Run `bctl ps neo4j` and `bctl logs --tail 100 neo4j`.
2. Common causes:
   - Out of memory: raise `NEO4J_MEMORY` and keep heap plus page cache about
     0.5 GB below it.
   - A full disk: see [disk-full](#disk-full).
   - With Aura, a suspended instance: resume it in the Aura console.
3. Run `bctl restart neo4j`. The backend recovers by itself within about 30 s,
   once the circuit breaker's probe succeeds.
4. If the data is corrupt, rebuild the graph from Postgres:
   `cd deploy && ./rebuild-graph.sh --accept-differences` (see OPERATIONS.md).
   Notes are never at risk.

## snapshot-stale
**`BrahmastraGraphSnapshotStale`: the outage copy has not refreshed in over 6 hours while the engine is up.**
Nothing is broken yet, but the next outage would show an old graph.

1. Run `bctl logs backend | grep -i snapshot`. The refresh job runs every
   `SNAPSHOT_REFRESH_MINUTES`, and after every pipeline run.
2. To force one: `bctl exec backend python -c "from brahmastra import graph_snapshot; print(graph_snapshot.refresh_all())"`.
3. A Postgres error here usually means a disk or permission problem; check
   Postgres' logs.

## pipeline-failing
**`BrahmastraPipelineFailing`: the last pipeline run in a workspace failed.** New notes are not reaching the graph.

1. Run `bctl exec backend curl -s localhost:8001/pipeline/status?workspace=WS`,
   and open the dashboard's Diagnostics tab.
2. The `failed_stages` field says where it failed:
   - `extract`: LLM quota or keys; see [llm-errors](#llm-errors).
   - `graph`: Neo4j; see [graph-engine-down](#graph-engine-down).
3. Failed notes retry automatically on the next run. To run one now:
   `bctl exec backend python -c "from brahmastra.pipeline import run_pipeline; print(run_pipeline()['status'])"`.

## llm-errors
**`BrahmastraLLMErrors`: more than 20% of LLM calls fail.** Extraction and Ask are affected.

1. Check the Grafana panel "LLM calls by outcome":
   - `quota`: the provider's limit is spent. Add keys (Diagnostics tab, admin
     only) or wait for the reset.
   - `unavailable`: a provider outage or a retired model. Check the provider's
     status page. A retired model needs a new `GROQ_MODEL` (CLAUDE.md, the
     "Groq retires hosted models" section).
   - `error`: run `bctl logs backend | grep -i llm` and find the failing
     request by `trace_id`.
2. A different provider can be selected deliberately: set `LLM_PROVIDER`, then
   `bctl up -d backend`.

## error-budget-burn
**`BrahmastraErrorBudgetFastBurn` / `SlowBurn`: API errors are spending the monthly budget too fast.**

1. In Grafana, check "Requests by status" and "Slowest routes" to see which
   route is failing.
2. Search the logs for that route's 5xx lines:
   `bctl logs --since 1h backend | grep '"status": 5'`.
3. If it started with a deploy, roll back: `cd deploy && ./upgrade.sh --rollback`.
4. If a dependency is the cause, see the matching section above.
5. When resolved, check the budget left. Policy rules apply
   (docs/RELIABILITY.md); a burn above 20% of the budget needs a postmortem.

## slow-api
**`BrahmastraLatencyBudgetBurn`: more than 1% of API requests take longer than 1.5 s.**

1. In Grafana, check "Slowest routes (p95)" for the routes involved.
2. Usual causes:
   - Neo4j slow: check its memory and page cache.
   - Postgres slow: check its CPU and disk.
   - The host saturated: check the CPU and memory panels.
   - A pipeline run competing for the database.
3. A run of statement timeouts (60 s) in the logs means a query needs an
   index or a fix; open an issue with the route and `trace_id`.

## slow-ask
**`BrahmastraAskLatencyBurn`: Ask answers are taking more than 10 s.**

1. Usually the LLM provider. Check "LLM calls by outcome" and the latency
   histograms.
2. If the graph engine is down, Ask uses the notes fallback: see
   [graph-engine-down](#graph-engine-down).
3. Consider a faster model or provider for Ask; it is a configuration change,
   not a code change.

## backup-failed
**`BrahmastraBackupFailed` / `Stale` / `NeverSucceeded`: today's backup did not complete.** Earlier backups are untouched.

1. Run `bctl logs --tail 50 backup`. The failing step is named: `pg_dump`,
   `verify`, `restic init`, `restic backup` or `restic retention`.
2. **The `restic` steps:** check the off-host storage. Are the credentials
   expired, the bucket deleted or the network blocked? Test the access:
   `bctl run --rm backup sh -c 'restic snapshots'`.
3. **The `pg_dump` step:** check Postgres' health and the free disk space on
   the backups volume.
4. Once fixed, back up now: `bctl run --rm backup backup`. Confirm the alert
   resolves.

## restore-test-failing
**`BrahmastraRestoreTestFailing`: the weekly restore test failed, or has not passed in 8 days.** Treat as urgent: backups may not be restorable.

1. Run `bctl logs backup | grep restore-test`. The failing step is named.
2. Run it by hand: `bctl run --rm backup restore-test`.
3. If the restored copy holds under 80% of the live notes, backups are missing
   recent data. Check the backup schedule and its last successes.
4. Do not close this until a restore test passes.

## disk-full
**`HostDiskFillingUp`: a filesystem is below 10% free or will fill within a day.**

1. Run `df -h` and `docker system df`.
2. Safe to reclaim:
   - old images: `docker image prune -a` (keep the current version and the one
     before it, for rollback);
   - local dumps beyond `BACKUP_KEEP_LOCAL_DAYS`;
   - the build cache: `docker builder prune`.
3. Never delete the `postgres-data` volume, or the active Neo4j volume (see
   `NEO4J_DATA_VOLUME`).
4. Prometheus data: lower `PROMETHEUS_RETENTION`.

## memory
**`HostMemoryLow`: less than 8% of memory is available.**

1. In Grafana, check "Container memory" for the biggest consumer.
2. Lower the per-service limits in `.env.production`, or move to a larger host
   (DEPLOY_VPS.md lists the sizes). Neo4j's heap and page cache are the usual
   lever.

## container-restarting
**`ContainerRestarting`: a service restarted more than twice in 30 minutes.**

1. Run `bctl logs --tail 100 <service>`, and `docker inspect` the container
   for `OOMKilled` and the exit code.
2. Exit code 78 is an unsafe-configuration refusal: read the listed problems.
3. A crash loop right after an upgrade: run
   `cd deploy && ./upgrade.sh --rollback`.

## watchdog
**`Watchdog` always fires; that is its job.** Alertmanager sends it every
minute to `WATCHDOG_URL`, a dead-man's switch that alarms when the pings stop.

If **the dead-man's switch** alerts, the alerting pipeline itself is broken:
check `bctl ps prometheus alertmanager` and the logs of both.

---

After any incident that spent more than 20% of the error budget, or caused data
loss or exposure, write a postmortem: `docs/POSTMORTEM_TEMPLATE.md`.
