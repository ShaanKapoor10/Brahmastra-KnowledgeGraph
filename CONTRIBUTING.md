# Contributing

Brahmastra is source-available under the [Elastic License 2.0](LICENSE). By
opening a pull request you agree that your contribution is licensed under the
same terms. Security issues go through [SECURITY.md](SECURITY.md), never an issue.

## The one rule for merging

Every change reaches `main` through a pull request, and the pull request merges
only when **`ci-success`** is green. That single check stands for every job in
`.github/workflows/ci.yml`: lint, types, tests, the security scans, the
production-image smoke test and the upgrade-path test. Force pushes and
deleting `main` are blocked, and release tags (`v*`) are protected.

## Setting up

```bash
# backend (Python 3.12, uv)
cd backend
uv sync --frozen                 # exactly the locked versions, every file hash-checked
uv run pytest tests/ -q          # the suite never touches real databases or LLMs

# dashboard (Node 24, pnpm via corepack)
cd frontend
corepack enable && pnpm install --frozen-lockfile
pnpm dev
```

The 14 Postgres integration tests need a throwaway database; see "Tests" in
`CLAUDE.md`. Without it they are skipped, and CI runs them.

## Checks to run before pushing

```bash
cd backend  && uv run ruff check . && uv run mypy && uv run pytest tests/ -q
cd frontend && pnpm exec tsc --noEmit && pnpm lint && pnpm build
deploy/tests/release-verify-test.sh
deploy/monitoring/tests/check-configs.sh      # needs Docker
```

## Dependencies

Dependencies are **locked and hash-pinned**, and installs are frozen: an
install either gets exactly the reviewed bytes or fails.

| | lock | change it with |
|---|---|---|
| Python | `backend/uv.lock` | `uv add <pkg>` / `uv lock --upgrade-package <pkg>` |
| dashboard | `frontend/pnpm-lock.yaml` | `pnpm add <pkg>` / `pnpm update <pkg>` |
| base and service images | `@sha256:` digests in Dockerfiles and compose | Dependabot |
| GitHub Actions | full commit SHAs | Dependabot |

- **torch comes only from the PyTorch CPU index**, and nothing else may
  (`[tool.uv.sources]` in `pyproject.toml`). Never resolve with
  `--index-strategy unsafe-best-match`: that is how dependency-confusion attacks
  get in.
- Dependabot opens grouped updates weekly, and waits 7 days after a release (30
  for a major) before proposing it. Security updates are not delayed.
- A new dependency must pass the licence allowlist
  (`.github/scripts/check_licenses.py`, decisions in `docs/LICENSES.md`).
- Vulnerability gates fail on advisories that have a fix. To accept one, add it
  to `.github/audit-ignore.toml` (pip, pnpm) or `.trivyignore.yaml` (images)
  with a reason and an expiry date; entries without both fail the build.

## Workflows

Workflows are linted by actionlint and zizmor in CI. Keep them that way:

- top-level `permissions: contents: read`, and more only on the job that needs it;
- actions pinned to a full commit SHA, with the version in a comment;
- `persist-credentials: false` on every checkout;
- never `pull_request_target`, and never `${{ }}` expressions inside `run:`
  (pass them through `env:`);
- no caches in the release workflow.

## Releasing

Tag a commit that is on `main` with `vMAJOR.MINOR.PATCH` and push the tag.
`release.yml` builds, signs and publishes; servers then run
`deploy/upgrade.sh <version>`. Schema changes must stay backward compatible
for one release (`docs/OPERATIONS.md`, *The migration rule*), which the
upgrade-path test checks.
