"""
Licence allowlist for everything that ships in a release (docs/LICENSES.md).

    python check_licenses.py python             # the running interpreter's packages
    python check_licenses.py npm licenses.json  # `pnpm licenses list --prod --json`

Fails when a package's licence is not on the allowlist, or is a conditional
licence (LGPL, MPL, CC-BY) on a package the review did not clear. A failure is
a prompt to REVIEW, not to widen the list: add a licence only after reading
what it obliges, and record the decision in docs/LICENSES.md.

Python metadata is uneven: License-Expression (SPDX, newer packages), a free
text License field, or only trove classifiers. They are tried in that order,
and free text is mapped through the table below or reported as unknown -- an
unknown licence fails rather than passing unread. Standard library only.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys

PERMISSIVE = {
    "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "ISC", "Apache-2.0",
    "BlueOak-1.0.0", "Python-2.0", "PSF-2.0", "CNRI-Python", "AFL-2.1", "CC0-1.0",
    "Unlicense", "Zlib", "BSL-1.0", "PostgreSQL",
}
EXCEPTIONS = {"LLVM-exception"}            # "Apache-2.0 WITH LLVM-exception" is permissive
# Conditional licences, allowed ONLY for the packages the review cleared.
CONDITIONAL = {
    "LGPL-3.0-only": ["psycopg", "psycopg-binary", "psycopg-c", "@img/sharp-*"],
    "LGPL-3.0-or-later": ["psycopg", "psycopg-binary", "@img/sharp-*"],
    "MPL-2.0": ["certifi", "tqdm"],
    "CC-BY-4.0": ["caniuse-lite"],
}
OUR_OWN = {"brahmastra-backend", "brahmastra", "frontend"}

# Free-text and classifier spellings seen in real metadata -> SPDX.
ALIASES = {
    "mit license": "MIT", "mit": "MIT",
    "bsd": "BSD-3-Clause", "bsd license": "BSD-3-Clause", "new bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause", "bsd 3-clause license": "BSD-3-Clause",
    "apache software license": "Apache-2.0", "apache license, version 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0", "apache 2.0 license": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "isc license": "ISC", "isc license (iscl)": "ISC",
    "psf": "PSF-2.0", "python software foundation license": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "public domain": "Unlicense",
}


def _atom(token: str) -> str:
    token = token.strip().strip("()").strip()
    return ALIASES.get(token.lower(), token)


def allowed(expression: str, package: str) -> bool:
    """Evaluate an SPDX-style expression: OR needs one acceptable side, AND needs all."""
    def ok_atom(lic: str) -> bool:
        base, _, exc = lic.partition(" WITH ")
        base = _atom(base)
        if exc and exc.strip() not in EXCEPTIONS:
            return False
        if base in PERMISSIVE:
            return True
        return any(fnmatch.fnmatch(package, pat) for pat in CONDITIONAL.get(base, []))

    expr = re.sub(r"\s+", " ", expression.strip())
    if ALIASES.get(expr.lower()):
        return ok_atom(expr)
    return any(all(ok_atom(part) for part in re.split(r"\s+AND\s+", alt.strip("() ")))
               for alt in re.split(r"\s+OR\s+", expr.strip("() ")))


def python_packages() -> list[tuple[str, str]]:
    import importlib.metadata as md

    out = []
    for dist in md.distributions():
        meta = dist.metadata
        name = meta["Name"]
        expr = (meta.get("License-Expression") or "").strip()
        if not expr:
            text = (meta.get("License") or "").strip()
            # A whole licence text pasted into the field is not an expression.
            if text and "\n" not in text and len(text) <= 80 and "copyright" not in text.lower():
                expr = text
        if not expr:
            classifiers = [c.split(" :: ")[-1] for c in (meta.get_all("Classifier") or [])
                           if c.startswith("License ::") and "OSI Approved" != c.split(" :: ")[-1]]
            expr = " OR ".join(classifiers)          # several classifiers = dual licensed
        out.append((name, expr or "UNKNOWN"))
    return out


def npm_packages(path: str) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [(pkg["name"], licence) for licence, pkgs in data.items() for pkg in pkgs]


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"python", "npm"} or (argv[0] == "npm" and len(argv) < 2):
        print(__doc__)
        return 2
    packages = python_packages() if argv[0] == "python" else npm_packages(argv[1])
    bad = sorted({(n, e) for n, e in packages if n.lower() not in OUR_OWN and not allowed(e, n.lower())})
    print(f"{len(packages)} {argv[0]} packages checked")
    for name, expr in bad:
        print(f"NOT ALLOWED  {name}: {expr}")
    if bad:
        print("Review each licence, then record the decision in docs/LICENSES.md and here.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
