"""
Prometheus metrics, in the standard text format, at GET /metrics.

INTERNAL ONLY. The bundled Prometheus (docker-compose.monitoring.yml) scrapes
backend:8001/metrics on the internal network, and a company's own tooling can
federate from it or scrape the backend from inside the network. Caddy refuses
/metrics at the edge (deploy/Caddyfile, tested), and the endpoint itself
refuses any request that came through a proxy (X-Forwarded-For present): two
layers, because metrics name routes, workspaces and failure modes.

WHAT IS MEASURED, and why each exists:

  brahmastra_http_requests_total{method,route,status_class}
  brahmastra_http_request_duration_seconds{method,route}     histogram
      `route` is the TEMPLATE (/notes/{note_id}), never the concrete path, so
      the label set stays bounded. Buckets cluster around the 1.5 s API
      latency objective (docs/RELIABILITY.md) so its percentile is precise.
  brahmastra_ask_duration_seconds{outcome}                   histogram
      /ask has its own objective: it waits on a model. outcome = graph,
      notes_fallback (graph engine down) or error.
  brahmastra_llm_calls_total{provider,outcome}, _llm_call_duration_seconds,
  brahmastra_llm_chars_total{provider,direction}
      Characters, not tokens: every provider counts tokens differently, and an
      honest character count divided by ~4 is a usable cost proxy.
  brahmastra_pipeline_runs_total{mode,status}, _pipeline_stage_duration_seconds{stage}
  -- computed at scrape time, across processes --
  brahmastra_graph_up                                  1 if the graph engine answers
  brahmastra_pipeline_last_success_timestamp_seconds{workspace}
  brahmastra_graph_snapshot_age_seconds{workspace}, _graph_snapshot_bytes{workspace}
  brahmastra_backup_last_success_timestamp_seconds, _backup_last_attempt_ok
  brahmastra_restore_test_last_success_timestamp_seconds, _restore_test_last_attempt_ok
  brahmastra_build_info{version}

MULTIPROCESS. The backend runs several uvicorn workers; each keeps its own
counters. With PROMETHEUS_MULTIPROC_DIR set (the container sets it, on tmpfs,
emptied at start) the scrape merges every worker's files. The scrape-time
gauges are computed fresh by whichever worker answers, from shared state
(Postgres, the run record on the data volume, the backup status files).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily

HTTP_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 5.0, 10.0, 30.0)
ASK_BUCKETS = (0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 60)
LLM_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60, 120)
STAGE_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 3600)

HTTP_REQUESTS = Counter("brahmastra_http_requests_total", "HTTP requests by route template and status class.",
                        ["method", "route", "status_class"])
HTTP_LATENCY = Histogram("brahmastra_http_request_duration_seconds", "HTTP request latency by route template.",
                         ["method", "route"], buckets=HTTP_BUCKETS)
ASK_LATENCY = Histogram("brahmastra_ask_duration_seconds", "End-to-end /ask latency by how it was answered.",
                        ["outcome"], buckets=ASK_BUCKETS)
LLM_CALLS = Counter("brahmastra_llm_calls_total", "LLM calls by provider and outcome.", ["provider", "outcome"])
LLM_LATENCY = Histogram("brahmastra_llm_call_duration_seconds", "LLM call latency by provider.", ["provider"],
                        buckets=LLM_BUCKETS)
LLM_CHARS = Counter("brahmastra_llm_chars_total", "Characters sent to and received from LLMs (tokens ~ chars/4).",
                    ["provider", "direction"])
LLM_TOKENS = Counter("brahmastra_llm_tokens_total",
                     "Tokens the provider reports billing: input, output and thoughts (billed as output).",
                     ["provider", "kind"])
PIPELINE_RUNS = Counter("brahmastra_pipeline_runs_total", "Pipeline runs by mode and outcome.", ["mode", "status"])
PIPELINE_STAGE = Histogram("brahmastra_pipeline_stage_duration_seconds", "Pipeline stage durations.", ["stage"],
                           buckets=STAGE_BUCKETS)

def _ops_state() -> Path:
    """Where the backup job writes its outcomes; read per scrape, not at import."""
    return Path(os.environ.get("BRAHMASTRA_OPS_STATE", "/ops-state"))


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, f"{status // 100}xx").inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


def observe_pipeline(result: dict[str, Any]) -> None:
    PIPELINE_RUNS.labels(result.get("mode") or "unknown", result.get("status") or "unknown").inc()
    for stage, secs in (result.get("timings_s") or {}).items():
        try:
            PIPELINE_STAGE.labels(stage).observe(float(secs))
        except (TypeError, ValueError):
            pass


def _iso_to_ts(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _status_file(name: str) -> dict[str, Any]:
    try:
        return json.loads((_ops_state() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class StateCollector:
    """Gauges computed at scrape time. Never raises: a broken probe is a missing sample."""

    def collect(self):
        version = os.environ.get("BRAHMASTRA_VERSION", "dev")
        info = InfoMetricFamily("brahmastra_build", "The running release.")
        info.add_metric([], {"version": version})
        yield info

        graph_up = GaugeMetricFamily("brahmastra_graph_up", "1 if the graph engine answers a query.")
        try:
            from brahmastra import db

            db.get_db_stats()
            graph_up.add_metric([], 1)
        except Exception:                                       # noqa: BLE001
            graph_up.add_metric([], 0)
        yield graph_up

        last_ok = GaugeMetricFamily("brahmastra_pipeline_last_success_timestamp_seconds",
                                    "When the graph last built successfully.", labels=["workspace"])
        last_status = GaugeMetricFamily("brahmastra_pipeline_last_run_ok",
                                        "1 if the most recent pipeline run succeeded.", labels=["workspace"])
        snap_age = GaugeMetricFamily("brahmastra_graph_snapshot_age_seconds",
                                     "Age of the stale-graph copy kept for outages.", labels=["workspace"])
        snap_bytes = GaugeMetricFamily("brahmastra_graph_snapshot_bytes",
                                       "Size of the stale-graph copy.", labels=["workspace"])
        try:
            from brahmastra import db, graph_snapshot, pipeline
            from brahmastra.rebuild import bound

            for ws in sorted(w["id"] for w in db.list_workspaces()):
                with bound(ws):
                    record = pipeline.last_run() or {}
                    ts = _iso_to_ts(record.get("last_success_at"))
                    if ts:
                        last_ok.add_metric([ws], ts)
                    if record.get("status"):
                        last_status.add_metric([ws], 1 if record["status"] == "ok" else 0)
                    meta = graph_snapshot.store().meta()
                    if meta:
                        snap_age.add_metric([ws], max(0.0, time.time() - float(meta["saved_at"])))
                        snap_bytes.add_metric([ws], float(meta["bytes"]))
        except Exception:                                       # noqa: BLE001
            pass
        yield from (last_ok, last_status, snap_age, snap_bytes)

        for prefix, name in (("backup", "backup.json"), ("restore_test", "restore_test.json")):
            state = _status_file(name)
            ok_ts = GaugeMetricFamily(f"brahmastra_{prefix}_last_success_timestamp_seconds",
                                      f"When the last {prefix.replace('_', ' ')} succeeded.")
            ok_now = GaugeMetricFamily(f"brahmastra_{prefix}_last_attempt_ok",
                                       f"1 if the most recent {prefix.replace('_', ' ')} succeeded.")
            if state.get("last_success"):
                ok_ts.add_metric([], float(state["last_success"]))
            if state.get("outcome"):
                ok_now.add_metric([], 1 if state["outcome"] == "ok" else 0)
            yield ok_ts
            yield ok_now


def render() -> tuple[bytes, str]:
    """The /metrics body: every worker's counters, plus the scrape-time gauges."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        base = CollectorRegistry()
        multiprocess.MultiProcessCollector(base)
    else:
        base = REGISTRY
    state = CollectorRegistry()
    state.register(StateCollector())
    return generate_latest(base) + generate_latest(state), CONTENT_TYPE_LATEST
