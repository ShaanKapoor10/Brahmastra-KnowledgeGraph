"""
Rebuild the derived graph from Postgres, and prove the result.

The graph is a cache; a rebuild is how it is recovered (after a restore), how
an ontology change reaches old notes, and how a corrupted graph is replaced.
This module answers the two questions a rebuild raises:

  DID IT REPRODUCE THE GRAPH?  A FINGERPRINT per workspace: every fact hashed
  by content -- lower(subject) | relation | lower(object) | source note -- and
  grouped by where its note came from (mcp, checkpoint, transcript,
  meeting-record, notion ...). Comparing two fingerprints says which kind of
  fact differs, not just that a count moved. Comparing counts alone is how the
  2026-10-13 gap read as "87%" until hashes showed it was exactly one meeting
  record's 251 facts.

  DID IT CHANGE ANYTHING IT SHOULD NOT?  Rebuilding twice must give the same
  fingerprint (tests/test_rebuild.py): a rebuild is idempotent, or it is a
  random walk.

Where the rebuild WRITES is decided by the deployment, not here.
deploy/rebuild-graph.sh points it at an empty shadow Neo4j, compares the
fingerprints, and only then swaps volumes, so readers never see a half-built
graph and a failed rebuild leaves the live one untouched.

    python -m brahmastra.rebuild --fingerprint            # JSON for every workspace
    python -m brahmastra.rebuild --rebuild-all            # rebuild every workspace here
    python -m brahmastra.rebuild --rebuild-all --expect - # ...and compare with a fingerprint on stdin
    python -m brahmastra.rebuild --fingerprint --expect - # does the live graph match a fingerprint/report?
Exit codes: 0 reproduced, 3 differences found, 1 a rebuild failed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator


def fact_hash(t: dict[str, Any]) -> str:
    parts = (str(t.get("subject_text") or "").strip().lower(), str(t.get("relation") or ""),
             str(t.get("object_text") or "").strip().lower(), str(t.get("source_note_id") or ""))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@contextmanager
def bound(workspace_id: str) -> Iterator[None]:
    from brahmastra.workspace import reset_request_workspace, set_request_workspace

    token = set_request_workspace(workspace_id)
    try:
        yield
    finally:
        reset_request_workspace(token)


def fingerprint() -> dict[str, Any]:
    """The bound workspace's graph, summarised by content."""
    from brahmastra import db

    source_of = {n["id"]: (n.get("source") or "(none)") for n in db.get_notes()}
    groups: dict[str, list[str]] = defaultdict(list)
    for t in db.get_all_triples():
        src = source_of.get(t.get("source_note_id") or "", "(no note)")
        groups[src].append(fact_hash(t))
    by_source = {}
    for src, hashes in sorted(groups.items()):
        digest = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
        by_source[src] = {"facts": len(hashes), "sha256": digest}
    every = sorted(h for hs in groups.values() for h in hs)
    return {"facts": len(every), "sha256": hashlib.sha256("\n".join(every).encode()).hexdigest(),
            "by_source": by_source}


def workspaces() -> list[str]:
    from brahmastra import db

    return sorted(w["id"] for w in db.list_workspaces())


def fingerprint_all() -> dict[str, Any]:
    out = {}
    for ws in workspaces():
        with bound(ws):
            out[ws] = fingerprint()
    return out


def rebuild_all() -> dict[str, Any]:
    """run_pipeline(full=True) in every workspace of the configured store."""
    from brahmastra.pipeline import run_pipeline

    report: dict[str, Any] = {}
    for ws in workspaces():
        with bound(ws):
            started = time.perf_counter()
            result = run_pipeline(full=True)
            report[ws] = {"status": result.get("status"), "failed_stages": result.get("failed_stages"),
                          "timings_s": result.get("timings_s"),
                          "seconds": round(time.perf_counter() - started, 1),
                          "fingerprint": fingerprint()}
    return report


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable differences between two fingerprint_all() results."""
    diffs = []
    for ws in sorted(set(expected) | set(actual)):
        e, a = expected.get(ws), actual.get(ws)
        if e is None or a is None:
            diffs.append(f"{ws}: only in the {'rebuild' if e is None else 'live graph'}")
            continue
        if e["sha256"] == a["sha256"]:
            continue
        for src in sorted(set(e["by_source"]) | set(a["by_source"])):
            es, as_ = e["by_source"].get(src, {"facts": 0}), a["by_source"].get(src, {"facts": 0})
            if es.get("sha256") != as_.get("sha256"):
                diffs.append(f"{ws}/{src}: {es['facts']} facts live, {as_['facts']} rebuilt"
                             + ("" if es["facts"] != as_["facts"] else " (same count, different content)"))
    return diffs


def _as_fingerprints(doc: dict[str, Any]) -> dict[str, Any]:
    """Accept either a --fingerprint document or a --rebuild-all report."""
    if "report" in doc:
        return {ws: r["fingerprint"] for ws, r in doc["report"].items()}
    return doc


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    ap = argparse.ArgumentParser(prog="python -m brahmastra.rebuild")
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--rebuild-all", action="store_true")
    ap.add_argument("--expect", help="a --fingerprint JSON file to compare against, or - for stdin")
    args = ap.parse_args(argv)
    logging.disable(logging.WARNING)

    expected = None
    if args.expect:
        raw = sys.stdin.read() if args.expect == "-" else open(args.expect, encoding="utf-8").read()
        expected = _as_fingerprints(json.loads(raw))
    if args.fingerprint:
        current = fingerprint_all()
        if expected is None:
            print(json.dumps(current, indent=1))
            return 0
        diffs = compare(expected, current)
        print(json.dumps({"differences": diffs}, indent=1))
        return 3 if diffs else 0
    if not args.rebuild_all:
        ap.print_help()
        return 2
    report = rebuild_all()
    failed = [ws for ws, r in report.items() if r["status"] != "ok"]
    result: dict[str, Any] = {"report": report, "failed": failed}
    if expected is not None:
        result["differences"] = compare(expected, {ws: r["fingerprint"] for ws, r in report.items()})
    print(json.dumps(result, indent=1))
    if failed:
        return 1
    return 3 if result.get("differences") else 0


if __name__ == "__main__":
    raise SystemExit(main())
