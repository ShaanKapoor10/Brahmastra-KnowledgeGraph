# Observability: logs, traces, metrics and monitoring

What the system records about itself, where it goes, how long it is kept, and
what it must never contain.

## Logs

**JSON lines on stdout** (`LOG_FORMAT=json`, the production default; `text`
for development), collected by Docker and rotated at 10 MB × 5 files per
service. One line per request, plus whatever the code logs.

A log line may carry **only these fields** (`brahmastra/logs.py`,
`ALLOWED_FIELDS`):

`ts level logger msg trace_id span_id event method route status duration_ms
workspace user_id identity error_type at stage component state count seconds
outcome version`

Anything else a developer passes is **dropped**. That is an allowlist, not a
denylist, so a new field cannot leak by default. Messages also pass through
the secret scrubber as a second layer.

**Never logged, by construction:**

| Never | Why | How it is prevented |
|---|---|---|
| request and response bodies | note text, transcripts and answers are customer content | no code path logs a body; the request log has fixed fields |
| query strings | search text lives there (`/notes?q=...`) | the request log records the **route template** (`/notes/{note_id}`); uvicorn's access log is off |
| headers | `Authorization`, cookies, API keys | never read into a log record; Caddy redacts credentials in its access log by default |
| prompts and model replies | they quote the notes | the LLM wrapper records counts and characters, never text |
| email addresses | personal data | identities are logged as opaque `user_id`s; the audit log (below) holds emails under access control |
| exception messages | they can quote whatever the failing code held | the exception's **type** and **location** are logged, not its message |

Tested: `backend/tests/test_logs.py` proves unknown fields, secrets, exception
text and query strings stay out.

### Log retention and privacy policy

| Record | Where | Retention | Contains personal data? | Access |
|---|---|---|---|---|
| Application logs | Docker json-file on the host | **rotation: 10 MB × 5 per service** (typically days), unless shipped elsewhere | opaque user ids, workspace ids, client IPs in Caddy logs | host administrators |
| Caddy access logs | stdout (same rotation) | same | **client IP addresses** (personal data under GDPR) | host administrators |
| Audit log (`audit_log` table) | Postgres, in backups | **kept with the data; review yearly**; delete entries older than **1 year** unless a legal hold applies | emails, IPs, actions | admins (`GET /v1/auth/audit`) |
| Prometheus metrics | `prometheus-data` volume | **30 days** (`PROMETHEUS_RETENTION`) | no: labels are route templates and workspace ids | over an SSH tunnel |
| Backups | local volume and off-host (restic) | 7 daily, 5 weekly, 12 monthly | **yes**: everything in Postgres | whoever holds `RESTIC_PASSWORD` |

**Rules:**
- Shipping logs to a central system (Loki, Elasticsearch, a SIEM) keeps these
  rules. Set retention there to **30 days**, or the organisation's policy if
  stricter, and restrict access to operators.
- **Deletion requests** (right to erasure): delete the user (admin API). Their
  audit entries are pseudonymised by deleting the user row; emails remain in
  audit rows until the yearly purge, which the data-protection notice must
  state. Backups age out under the retention above; do not edit backups.
- **Never** raise the log level to DEBUG in production for longer than an
  investigation. Use a `trace_id` to follow one request instead.
- The audit purge is manual for now:
  `bctl exec postgres psql -U brahmastra -c "DELETE FROM audit_log WHERE at < extract(epoch from now() - interval '1 year')"`.

## Traces

W3C Trace Context (`traceparent`):
- **The dashboard's server** starts a trace for every backend call, or
  continues an incoming one.
- **The backend** logs `trace_id` and `span_id` on every line and returns
  `traceparent` on the response.

To follow a request: take the `trace_id` from any line, then grep every
service's logs for it. Exporting spans to an OpenTelemetry collector (Tempo,
Jaeger, a vendor) is a later step; the ids are already compatible.

## Metrics

`GET /metrics` on the backend serves Prometheus text format. The full list and
the reasons are in `brahmastra/metrics.py`:
- requests and latency by route template;
- `/ask` latency by outcome;
- LLM calls, latency and volume;
- pipeline runs and stage times;
- graph up, last successful build, and the outage-snapshot age and size;
- backup and restore-test last success.

**Internal only**, in two layers: Caddy refuses `/metrics` and `/v1/metrics`
(`deploy/tests/edge-test.sh`), and the endpoint refuses any proxied request.
The backend runs several workers in Prometheus multiprocess mode.

**Using your organisation's existing monitoring:**
- **Federate** from the bundled Prometheus (`/federate`), or add a
  `remote_write:` block to `deploy/monitoring/prometheus.yml`.
- Or run your agent on the `brahmastra_internal` Docker network and scrape
  `backend:8001/metrics` directly.

## The monitoring bundle (opt-in)

Enable it with `COMPOSE_PROFILES=monitoring` in `deploy/.env.production`, set
`ALERT_RECEIVER` and `ALERT_WEBHOOK_URL`, then `docker compose ... up -d`.

| Service | Purpose | Reach it |
|---|---|---|
| Prometheus | metrics, recording and alert rules (files in git, `promtool`-tested) | `127.0.0.1:9090` |
| Alertmanager | routes alerts to Slack, Teams or a webhook; refuses to start without a receiver | internal |
| Grafana | the "Brahmastra overview" dashboard (a file in git), datasource provisioned | `127.0.0.1:3002`, user `admin`, password `GRAFANA_ADMIN_PASSWORD` (generated by setup.sh) |
| node_exporter | host CPU, memory and disk | internal |
| cAdvisor | per-container CPU and memory | internal |

Prometheus and Grafana bind to **localhost only**. Reach them from your
machine with an SSH tunnel:

```bash
ssh -L 3002:127.0.0.1:3002 -L 9090:127.0.0.1:9090 ops@your-server
# then open http://localhost:3002
```

**cAdvisor runs privileged.** It must read every container's cgroups and
Docker's state. It is the one documented exception to the hardening in the
compose file; it publishes nothing and sits on the monitoring network only.

**Keep the external uptime check** (UptimeRobot, Better Stack, Pingdom) on
`https://DOMAIN/healthz` even with the bundle on. If the server dies, the
bundle dies with it, and only something outside can tell you. Route the
always-firing `Watchdog` alert to a dead-man's switch (`WATCHDOG_URL`) for the
same reason: it catches alerting itself being broken.

Dashboards and rules are **files in git**: change the JSON or YAML (the
dashboard is generated by `deploy/monitoring/grafana/build_dashboard.py`),
test it (`promtool test rules deploy/monitoring/tests/alerts.test.yml`) and
commit. Edits made in Grafana's UI are not kept.
