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
# then rebuild the graph from the restored notes (next section)
```

`restore --from` accepts `latest`, a file name from `restore --list`, or a
restic snapshot id. To restore onto a **new** server: run `setup.sh` with the
same `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` and storage keys, then follow the
steps above.

Measured on the trial install (6.9 MB dump, 160 notes): the restore took
**3 seconds**. Most of a recovery is the graph rebuild.

## Rebuilding the graph

```bash
bctl exec backend python -c "from brahmastra.pipeline import run_pipeline; print(run_pipeline(full=True)['status'])"
```

**Measured** on 13 Oct 2026, after restoring a copy of a real 160-note
database into an empty graph (2 vCPU-class container, local Neo4j):

| | |
|---|---|
| Time | **123 s** |
| Result | 1,572 facts, 1,432 entity groups, status `ok` |
| LLM cost | low: most extraction was served from the reply cache, which is in the backup |

Expect roughly **1 s per note warm**. A cold rebuild (cache lost, or the
extraction model changed) calls the LLM once per note and is bounded by the
provider's rate limit. On Groq's free tier that is several minutes per hundred
notes, and failed notes retry on the next run.

**Known gap:** the restored install held 1,805 facts before the rebuild and
1,572 after it (87%). The likely difference is records written directly during
meeting ingestion rather than extracted from notes. Until that is settled,
treat "rebuild from notes" as restoring the note-derived graph, and re-process
meeting transcripts (`/ingest/transcripts/{id}/reprocess`) after a recovery.

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
