"""
Gate on dependency vulnerabilities that can be FIXED.

    python audit_gate.py pip  pip-audit.json    # pip-audit -f json
    python audit_gate.py pnpm pnpm-audit.json   # pnpm audit --prod --json
    python audit_gate.py trivyignore .trivyignore.yaml   # validate Trivy's ignore file

A finding fails the build when a fixed version exists: that is work we can do
today. One with no fix is printed and does not fail, because no change we
could make would clear it; the nightly run brings it back the day a fix lands.

A missing or empty report FAILS: a scanner that crashed must never read as
"no findings".

Exceptions live in .github/audit-ignore.toml, and every entry must carry a
`reason` and an `expires` date. An entry without either, or past its date,
FAILS the build: an ignore is a decision with a review date, not a mute switch.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import tomllib

IGNORE_FILE = pathlib.Path(__file__).resolve().parents[1] / "audit-ignore.toml"


def load_ignores(today: dt.date) -> tuple[set[str], list[str]]:
    if not IGNORE_FILE.exists():
        return set(), []
    data = tomllib.loads(IGNORE_FILE.read_text(encoding="utf-8"))
    ids: set[str] = set()
    problems: list[str] = []
    for entry in data.get("ignore", []):
        vid = str(entry.get("id", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        expires = entry.get("expires")
        if not vid:
            problems.append(f"an ignore entry has no id: {entry}")
            continue
        if len(reason) < 20:
            problems.append(f"{vid}: an ignore needs a real reason (why it does not affect us)")
        if not isinstance(expires, dt.date):
            problems.append(f"{vid}: an ignore needs an expires = YYYY-MM-DD date")
        elif expires < today:
            problems.append(f"{vid}: the ignore expired on {expires}; review it again and extend or fix")
        ids.add(vid.upper())
    return ids, problems


def pip_findings(report: dict) -> list[dict]:
    out = []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []):
            out.append({"package": dep["name"], "version": dep.get("version", "?"), "id": v["id"],
                        "aliases": v.get("aliases", []), "fix": ", ".join(v.get("fix_versions", [])),
                        "severity": "?"})
    return out


def pnpm_findings(report: dict) -> list[dict]:
    out = []
    for adv in (report.get("advisories") or {}).values():
        patched = (adv.get("patched_versions") or "").strip()
        out.append({"package": adv.get("module_name", "?"),
                    "version": ", ".join(sorted({f.get("version", "?") for f in adv.get("findings", [])})) or "?",
                    "id": adv.get("github_advisory_id") or str(adv.get("id")),
                    "aliases": adv.get("cves", []),
                    "fix": "" if patched in {"", "<0.0.0"} else patched,
                    "severity": adv.get("severity", "?")})
    return out


def check_trivyignore(path: str, today: dt.date) -> int:
    """Trivy enforces expired_at itself; this adds that every entry HAS one, and a statement."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    entries = [e for e in re.split(r"^\s*-\s+id:", text, flags=re.M)[1:]]
    problems = []
    for entry in entries:
        vid = entry.splitlines()[0].strip()
        statement = re.search(r"^\s*statement:\s*(.+)$", entry, flags=re.M)
        expires = re.search(r"^\s*expired_at:\s*(\d{4}-\d{2}-\d{2})", entry, flags=re.M)
        if not statement or len(statement.group(1).strip().strip("\"'")) < 20:
            problems.append(f"{vid}: needs a statement saying why it does not affect us")
        if not expires:
            problems.append(f"{vid}: needs expired_at: YYYY-MM-DD")
        elif dt.date.fromisoformat(expires.group(1)) < today:
            problems.append(f"{vid}: expired on {expires.group(1)}; review it again")
    for p in problems:
        print(f"TRIVY IGNORE  {p}")
    print(f"trivyignore: {len(entries)} entries, {len(problems)} problems")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"pip", "pnpm", "trivyignore"}:
        print(__doc__)
        return 2
    if argv[0] == "trivyignore":
        return check_trivyignore(argv[1], dt.date.today())
    path = pathlib.Path(argv[1])
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    report = json.loads(text) if text else {}
    expected = "dependencies" if argv[0] == "pip" else "metadata"
    if expected not in report:
        print(f"{argv[0]}: no usable report in {argv[1]} -- the scanner failed, which is not a pass")
        return 1
    findings = pip_findings(report) if argv[0] == "pip" else pnpm_findings(report)
    ignored, problems = load_ignores(dt.date.today())

    failing = 0
    for f in findings:
        names = {f["id"].upper(), *(a.upper() for a in f["aliases"])}
        if names & ignored:
            state = "IGNORED (see audit-ignore.toml)"
        elif f["fix"]:
            state = f"FIXABLE -> {f['fix']}"
            failing += 1
        else:
            state = "no fix released yet"
        print(f"{state:40} {f['package']} {f['version']}  {f['id']}  severity={f['severity']}")
    for p in problems:
        print(f"IGNORE FILE  {p}")
    print(f"{argv[0]}: {len(findings)} findings, {failing} fixable and not ignored, "
          f"{len(problems)} ignore-file problems")
    return 1 if failing or problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
