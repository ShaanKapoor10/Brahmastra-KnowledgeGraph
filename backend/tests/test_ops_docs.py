"""
Every alert links a runbook section that exists, and the objectives in
docs/RELIABILITY.md are the numbers the alert rules use.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "deploy" / "monitoring" / "rules" / "brahmastra.rules.yml"
RUNBOOK = REPO / "docs" / "RUNBOOK.md"


def _alerts():
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g["rules"] if "alert" in r]


def _anchors():
    return {"-".join(re.findall(r"[a-z0-9]+", line[3:].lower()))
            for line in RUNBOOK.read_text(encoding="utf-8").splitlines() if line.startswith("## ")}


def test_every_alert_links_a_runbook_section_that_exists():
    anchors = _anchors()
    for alert in _alerts():
        url = alert.get("annotations", {}).get("runbook_url", "")
        assert url, f"{alert['alert']} has no runbook_url"
        anchor = url.rsplit("#", 1)[-1]
        assert anchor in anchors, f"{alert['alert']} links #{anchor}, which RUNBOOK.md does not have"


def test_every_alert_has_a_severity_and_a_summary():
    for alert in _alerts():
        assert alert.get("labels", {}).get("severity") in {"critical", "warning", "none"}, alert["alert"]
        assert alert.get("annotations", {}).get("summary"), alert["alert"]


def test_the_rules_use_the_objectives_the_reliability_doc_states():
    rules = RULES.read_text(encoding="utf-8")
    doc = (REPO / "docs" / "RELIABILITY.md").read_text(encoding="utf-8")
    assert "**99.5%** of requests succeed" in doc and "* 0.005)" in rules         # availability budget 0.5%
    assert "**99%** within 1.5 s" in doc and 'le="1.5"' in rules and "* 0.01)" in rules
    assert "**95%** within 10 s" in doc and 'le="10.0"' in rules
