"""Generate dashboards/brahmastra-overview.json. Edit THIS file, then run it:

    python deploy/monitoring/grafana/build_dashboard.py

Generated rather than hand-edited so every panel uses the same SLI selectors as
the alert rules (monitoring/rules/brahmastra.rules.yml) and the objectives in
docs/RELIABILITY.md -- a dashboard that measures something slightly different
from the alert is how a team ends up trusting neither.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DS = {"type": "prometheus", "uid": "prometheus"}
API = 'route!~"/health.*|/metrics|/ask|/pipeline/.*|/ingest/transcripts.*"'
UPDOWN = [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"},
                                        "1": {"text": "UP", "color": "green"}}}]
RUNBOOK = "https://github.com/ShaanKapoor10/vaultgraph-qy/blob/product/launch/docs/RUNBOOK.md"

panels: list[dict] = []
y = 0


def steps(*pairs):
    return [{"color": c, "value": v} for c, v in pairs]


def stat(title, expr, x, w, unit="", thresholds=None, desc="", mappings=None):
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": 4},
            "targets": [{"refId": "A", "expr": expr, "datasource": DS}],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background",
                        "graphMode": "none"},
            "fieldConfig": {"defaults": {"unit": unit, "mappings": mappings or [],
                                         "thresholds": {"mode": "absolute",
                                                        "steps": thresholds or steps(("green", None))}},
                            "overrides": []}}


def ts(title, targets, x, w, unit="", desc="", h=8):
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": legend, "datasource": DS}
                        for i, (e, legend) in enumerate(targets)],
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10}},
                            "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}


def row(title):
    global y
    panels.append({"type": "row", "title": title, "collapsed": False,
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
    y += 1


errors_30d = (f'sum(increase(brahmastra_http_requests_total{{{API},status_class="5xx"}}[30d]))'
              f' / clamp_min(sum(increase(brahmastra_http_requests_total{{{API}}}[30d])), 1)')

row("Service health  (objectives: docs/RELIABILITY.md)")
panels += [
    stat("API", 'up{job="brahmastra"}', 0, 4, mappings=UPDOWN, thresholds=steps(("red", None), ("green", 1)),
         desc="Prometheus can scrape the backend."),
    stat("Graph engine", "brahmastra_graph_up", 4, 4, mappings=UPDOWN,
         thresholds=steps(("orange", None), ("green", 1)),
         desc="When down the product is DEGRADED: notes, search and Ask work; the dashboard shows the saved graph."),
    stat("Availability (30 d)", f"1 - ({errors_30d})", 8, 4, unit="percentunit",
         thresholds=steps(("red", None), ("orange", 0.99), ("green", 0.995)), desc="Objective: 99.5% of API requests succeed."),
    stat("Error budget left (30 d)", f"1 - (({errors_30d}) / 0.005)", 12, 4, unit="percentunit",
         thresholds=steps(("red", None), ("orange", 0.25), ("green", 0.5)),
         desc="Share of the 0.5% monthly error budget not yet spent. Exhausted = feature freeze (error-budget policy)."),
    stat("API p95 latency", "brahmastra:api_p95_latency_seconds:rate5m", 16, 4, unit="s",
         thresholds=steps(("green", None), ("orange", 1), ("red", 1.5)), desc="Objective: 99% of API requests within 1.5 s."),
    stat("Ask p95 latency", "histogram_quantile(0.95, sum by (le) (rate(brahmastra_ask_duration_seconds_bucket[30m])))",
         20, 4, unit="s", thresholds=steps(("green", None), ("orange", 8), ("red", 10)),
         desc="Objective: 95% of answers within 10 s."),
]
y += 4
panels += [
    ts("Requests by status", [(f"sum by (status_class) (rate(brahmastra_http_requests_total{{{API}}}[5m]))",
                               "{{status_class}}")], 0, 8, unit="reqps"),
    ts("API latency", [(f"histogram_quantile({q}, sum by (le) (rate(brahmastra_http_request_duration_seconds_bucket{{{API}}}[5m])))",
                        f"p{int(q * 100)}") for q in (0.5, 0.95, 0.99)], 8, 8, unit="s"),
    ts("Slowest routes (p95)", [(f"topk(5, histogram_quantile(0.95, sum by (le, route) "
                                 f"(rate(brahmastra_http_request_duration_seconds_bucket{{{API}}}[15m]))))", "{{route}}")],
       16, 8, unit="s"),
]
y += 8

row("Ask and the LLM")
panels += [
    ts("Ask answers by outcome", [("sum by (outcome) (rate(brahmastra_ask_duration_seconds_count[15m]))", "{{outcome}}")],
       0, 8, unit="reqps", desc="notes_fallback = answered from notes because the graph engine was down."),
    ts("LLM calls by outcome", [("sum by (provider, outcome) (rate(brahmastra_llm_calls_total[15m]))",
                                 "{{provider}} {{outcome}}")], 8, 8, unit="reqps"),
    ts("LLM volume (est. tokens/min = chars / 4)", [("sum by (provider, direction) (rate(brahmastra_llm_chars_total[15m])) * 60 / 4",
                                                     "{{provider}} {{direction}}")], 16, 8, unit="short"),
]
y += 8

row("Data safety")
panels += [
    stat("Last good backup", "time() - brahmastra_backup_last_success_timestamp_seconds", 0, 6, unit="s",
         thresholds=steps(("green", None), ("orange", 26 * 3600), ("red", 30 * 3600)), desc="Alert above 26 h."),
    stat("Last restore test passed", "time() - brahmastra_restore_test_last_success_timestamp_seconds", 6, 6, unit="s",
         thresholds=steps(("green", None), ("red", 8 * 86400)), desc="Weekly; alert above 8 days."),
    stat("Outage copy of the graph (age)", "max(brahmastra_graph_snapshot_age_seconds)", 12, 6, unit="s",
         thresholds=steps(("green", None), ("orange", 6 * 3600))),
    stat("Pipeline: last success", "time() - min(brahmastra_pipeline_last_success_timestamp_seconds)", 18, 6, unit="s",
         thresholds=steps(("green", None), ("orange", 86400))),
]
y += 4
panels += [
    ts("Pipeline stage duration (p95)", [("histogram_quantile(0.95, sum by (le, stage) "
                                          "(rate(brahmastra_pipeline_stage_duration_seconds_bucket[6h])))", "{{stage}}")],
       0, 12, unit="s"),
    ts("Pipeline runs", [("sum by (status) (increase(brahmastra_pipeline_runs_total[1h]))", "{{status}}")], 12, 12),
]
y += 8

row("Host and containers")
svc = 'container_label_com_docker_compose_service'
panels += [
    ts("Container memory", [(f'sum by ({svc}) (container_memory_working_set_bytes{{{svc}!=""}})', "{{" + svc + "}}")],
       0, 8, unit="bytes"),
    ts("Container CPU", [(f'sum by ({svc}) (rate(container_cpu_usage_seconds_total{{{svc}!=""}}[5m]))', "{{" + svc + "}}")],
       8, 8, unit="percentunit"),
    ts("Disk free", [('node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|9p|nfs4?|cifs|fuse.*|rootfs"} '
                      '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|9p|nfs4?|cifs|fuse.*|rootfs"}', "{{mountpoint}}")],
       16, 8, unit="percentunit"),
]

for i, p in enumerate(panels, start=1):
    p["id"] = i
dashboard = {"uid": "brahmastra-overview", "title": "Brahmastra overview", "tags": ["brahmastra"],
             "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "30s",
             "time": {"from": "now-6h", "to": "now"},
             "links": [{"title": "Runbook", "type": "link", "url": RUNBOOK, "targetBlank": True}],
             "panels": panels}
out = HERE / "dashboards" / "brahmastra-overview.json"
out.write_text(json.dumps(dashboard, indent=1) + "\n", encoding="utf-8")
print(f"wrote {out.name}: {len(panels)} panels")
