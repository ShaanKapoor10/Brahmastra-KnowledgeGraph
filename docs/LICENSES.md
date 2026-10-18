# Licences

## Brahmastra itself

Brahmastra is **source-available under the [Elastic License 2.0](../LICENSE)**
(SPDX `Elastic-2.0`). Anyone may read, use, modify and self-host it. Three things
are not permitted:
- offering it to third parties as a hosted or managed service;
- circumventing any licence-key functionality;
- removing the licensing notices.

Copyright is in [`NOTICE`](../NOTICE). This summary is not legal advice; the
licence text governs.

## Third-party components: review of 2026-10-18

Every dependency that ships in a release was checked: the backend's Python
packages (104, from `backend/.venv`), the dashboard's production npm closure
(396 packages), and the service images run beside the app. The review is
repeated whenever dependencies change. CI's licence check fails on a new
licence outside the allowlist below.

### Allowed without conditions (permissive)

MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC, Apache-2.0 (also `WITH
LLVM-exception`, in torch), BlueOak-1.0.0, Python-2.0, PSF-2.0, CNRI-Python,
AFL-2.1 (dual-licensed with BSD-3-Clause), CC0-1.0, Unlicense, Zlib and BSL-1.0
(both inside numpy and torch), PostgreSQL. These are 98% of all packages.

The check is [`.github/scripts/check_licenses.py`](../.github/scripts/check_licenses.py),
run by the security workflow on every pull request and nightly. It reads each
package's SPDX expression (or maps its free-text licence and classifiers), and
an unrecognised licence FAILS rather than passing unread. The conditional
licences below are allowed only for the packages named there.

### Allowed with conditions, all of which we meet

| Component | Licence | How it is used | Obligation, and how it is met |
|---|---|---|---|
| `psycopg`, `psycopg-binary` (Postgres driver) | LGPL-3.0-only | imported unmodified as a separate, replaceable library | keep its licence notice and let users replace the library; it is an ordinary pip package in the image |
| `@img/sharp-*` (image library inside Next.js) | Apache-2.0 AND LGPL-3.0-or-later (libvips) | unmodified, dynamically loaded | as above |
| `certifi` | MPL-2.0 | unmodified | MPL is per file; obligations apply only to modified MPL files, and none are modified |
| `tqdm` | MPL-2.0 AND MIT | unmodified | as above |
| `caniuse-lite` | CC-BY-4.0 | browser-support data used at build time | attribution, which is kept in the package |

### Services run beside the app (separate containers, not linked)

| Service | Licence | Why it is fine | Watch for |
|---|---|---|---|
| **Neo4j Community** (`neo4j:5.26-community`) | **GPL-3.0** | runs unmodified in its own container and is reached over the Bolt network protocol. The app does not link it, and the Python driver (`neo4j`) is Apache-2.0. Shipping a compose file that pulls the official image is not distributing a modified GPL work. | do **not** fork or modify Neo4j's code. The Community edition's own terms also allow only a single instance, with no clustering; scaling past one instance means Neo4j Enterprise (commercial) or another engine |
| **Grafana OSS** (monitoring profile) | **AGPL-3.0** | unmodified official image, run separately and optional. AGPL obligations (offering the source) attach to *modified* versions used over a network. | do not modify Grafana's source. Dashboards and provisioning files are our own configuration, not a modification |
| Prometheus, Alertmanager, node_exporter, cAdvisor | Apache-2.0 | | |
| PostgreSQL + pgvector | PostgreSQL License (permissive) | | |
| Caddy | Apache-2.0 | | |
| restic (in the backup image) | BSD-2-Clause | | |

### Removed during the review

- `@vercel/analytics` (MPL-2.0): a leftover of the original Vercel template that
  rendered an analytics component in every production build. A self-hosted
  product must not carry third-party telemetry. Outside Vercel it would also
  only have produced failed requests.

### Our own package metadata

`backend/pyproject.toml` declares `license = "Elastic-2.0"`, so scanners report
our package correctly instead of "UNKNOWN".
