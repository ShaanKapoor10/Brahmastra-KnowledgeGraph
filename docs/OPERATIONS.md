# Operating Brahmastra

Day-two tasks for a production install made with `deploy/setup.sh`: backups,
restores, upgrades, rollbacks, rebuilding the graph, and the rules that keep
those safe. All commands run from the repository root on the server.

```bash
alias bctl='docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production'
```

## What holds what

| Data | Where | Backed up | If lost |
|---|---|---|---|
| Notes, workspaces, transcripts, accounts, audit log | Postgres (`postgres-data`) | **yes**, nightly | restore from backup |
| The knowledge graph (facts, entities, topics) | Neo4j (`neo4j-data`) | no, by design | rebuild from the notes |
| Model-reply cache | Postgres | yes (with the notes) | rebuilt on demand, at LLM cost |
| TLS certificates | `caddy-data` | no | re-issued automatically |

The graph is derived data: every fact is a function of the notes. Backing it up
would double the backup surface for something a rebuild recreates.

## Backups

The `backup` container runs on a schedule set in `deploy/.env.production`:

- **Daily at `BACKUP_TIME`.** It takes a `pg_dump` and checks that the dump can
  be read back. It keeps a local copy for `BACKUP_KEEP_LOCAL_DAYS` and sends an
  **encrypted** copy off the host with restic (AES-256, encrypted before it
  leaves the server). Off-host retention is daily, weekly and monthly
  (`BACKUP_KEEP_*`).
- **Weekly restore test** (`RESTORE_TEST_WEEKDAY` at `RESTORE_TEST_TIME`). It
  fetches the newest *off-host* copy, restores it into a throwaway Postgres, and
  checks the restored notes against the live database.
- **Alerting.** Any failure posts to `ALERT_WEBHOOK_URL`, which can be Slack or
  Microsoft Teams. Each success pings `BACKUP_HEARTBEAT_URL` /
  `RESTORE_TEST_HEARTBEAT_URL`, so a monitor (healthchecks.io, Better Stack,
  Cronitor) raises an alarm if the job stops running at all. Set up both:
  alerts catch failures, and heartbeats catch silence.

```bash
bctl run --rm backup backup          # back up now
bctl run --rm backup restore-test    # prove the latest backup restores
bctl run --rm backup restore --list  # local dumps and off-host snapshots
bctl logs --tail 50 backup
```

**`RESTIC_PASSWORD` is the key to every backup.** Keep a copy in your password
manager, away from this host. Without it the off-host copies cannot be
decrypted, by anyone.

## Restoring (disaster recovery)

```bash
bctl stop backend frontend scheduler
bctl run --rm backup restore --from latest --yes     # or a dump name / snapshot id
bctl up -d
cd deploy && ./rebuild-graph.sh --accept-differences   # rebuild the graph from the restored notes
```

`restore --from` accepts `latest`, a file name from `restore --list`, or a
restic snapshot id. To restore onto a **new** server: run `setup.sh` with the
same `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` and storage keys, then follow the
steps above.

Measured on the trial install (6.9 MB dump, 160 notes): the restore took
**3 seconds**. Most of a recovery is the graph rebuild.

## Rebuilding the graph

```bash
cd deploy && ./rebuild-graph.sh                       # rebuild, verify, swap
./rebuild-graph.sh --accept-differences               # after a restore or an ontology change
./rebuild-graph.sh --rollback                         # back to the previous graph
```

**Blue/green, so readers never see a half-built graph.** The live graph keeps
serving from one Neo4j volume. Meanwhile the script:

1. **Fingerprints the live graph.** Every fact is hashed by content and grouped
   by where its note came from.
2. **Rebuilds every workspace into an empty shadow Neo4j** on the other volume.
   The rebuild only reads Postgres: Notion and the checkpoint queue are off for
   it.
3. **Compares the fingerprints per note source.** A difference stops the swap
   unless you pass `--accept-differences`.
4. **Swaps volumes, then checks** the graph now serving is the one that was
   built. On any failure it switches back.

The previous graph stays on its volume for `--rollback` until the next
rebuild. A rebuild needs room for a second Neo4j (`NEO4J_MEMORY`) while it runs.

After a disaster recovery the live graph is empty, so the comparison
necessarily differs: use `--accept-differences`.

**What a rebuild reproduces.** Every fact must be recomputable from Postgres.
On 13 Oct 2026 a content-hash comparison of a real 1,835-fact graph against a
rebuild found:
- every fact extracted from notes (mcp, checkpoint, transcript, Notion and
  untagged notes) reproduced **exactly** (1,584/1,584);
- the only gap was one meeting record's 251 facts, which ingestion writes
  straight into the graph.

Those are now regenerated from the stored meeting findings
(`brahmastra/ingest/records.py`, with no model calls). A test
(`tests/test_graph_write_contract.py`) fails if any new code writes graph
facts that a rebuild cannot reproduce.

**Time.** 123 s for 160 notes inside the stack with a warm reply cache.
Every pipeline result now reports per-stage `timings_s`. A cold rebuild (cache
lost, or the extraction model changed) calls the LLM once per note and is
bounded by the provider's rate limit.

Run inside the backend only to rebuild in place (development; single-store
SQLite):

```bash
bctl exec backend python -m brahmastra.rebuild --rebuild-all
bctl exec backend python -m brahmastra.rebuild --fingerprint      # content fingerprint, per workspace
```

## Upgrades

```bash
cd deploy && ./upgrade.sh 0.3.0      # to the release tagged v0.3.0
./upgrade.sh --rollback              # back to the version before the last upgrade
```

`upgrade.sh`:
1. Refuses to start if another upgrade is running, the checkout has local
   changes, the tag does not exist, the config is invalid, or disk space is low.
2. **Takes a backup.** If the backup fails, the upgrade does not start.
3. Builds images tagged with the new version. If the build fails, nothing
   changes.
4. Switches `BRAHMASTRA_VERSION` and restarts. Schema migrations run as the
   backend starts.
5. Waits for the health check. If it fails, it **puts the previous version
   back**, checks again, and alerts either way.

Every upgrade and rollback is recorded in `deploy/upgrade-history.log`.
Versions are always explicit: no image uses `:latest`, so a restart never
changes what runs.

## The migration rule: backward compatible, always

Automatic rollback restores the previous *code*, not the previous *data*.
That is safe only if the previous version still works on the migrated schema,
so every schema change must be **backward compatible** (expand, then contract):

- **Allowed in a release:** new tables; new nullable columns, or columns with a
  default; new indexes; new values a reader can ignore.
- **Not in the same release:** dropping or renaming a column or table,
  tightening a constraint, changing a column's meaning. Do these in two steps:
  1. Release N writes both the old and the new form and reads the new one.
  2. Release N+1, or later, removes the old form, once no version that needs it
     can be rolled back to.
- Migrations are idempotent (`CREATE ... IF NOT EXISTS`, `ADD COLUMN IF NOT
  EXISTS`) and run at startup, so a half-applied upgrade finishes on the next
  start.

If a release has to break this rule, its release notes must say so, and
`--rollback` must be paired with restoring the pre-upgrade backup.

## Health and monitoring

| URL | Who | Says |
|---|---|---|
| `https://DOMAIN/healthz` | an uptime monitor (public) | up or down only |
| `bctl exec backend curl -s localhost:8001/health/ready` | operators | backends, auth mode, counts |
| `GET /v1/auth/audit` | admins | sign-ins, failures, lockouts, account changes |

Point an uptime monitor at `/healthz` with a 1-minute interval, and alert after
2 failures.

## Logs

Container logs rotate at 10 MB × 5 files per service (`x-logging` in the
compose file). Caddy writes JSON access logs.

```bash
bctl logs -f --tail 100 backend
```
