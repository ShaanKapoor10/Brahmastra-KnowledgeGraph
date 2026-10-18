# Security policy

## Reporting a vulnerability

**Do not open a public issue.** Report it privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
Only the maintainers can see the report.

Please include what is affected (version or commit), how to reproduce it, and
what an attacker gains. A proof of concept helps; access to anyone else's data
does not, so test against your own install.

What to expect:

| | |
|---|---|
| acknowledgement | within 3 working days |
| first assessment (severity, affected versions) | within 10 working days |
| fix for critical and high severity | target 30 days, with a release and an advisory |
| credit | in the advisory, unless you prefer otherwise |

## Supported versions

Security fixes go into the latest release. Upgrades are one command
(`deploy/upgrade.sh <version>`, with automatic rollback), so older releases are
not patched separately.

## Scope

In scope: the backend API, the dashboard, the MCP server, the deployment in
`deploy/` (compose, Caddy, scripts), and the release pipeline.

Out of scope: findings that need an attacker who already controls the host or
`deploy/.env.production`; denial of service by volume; vulnerabilities in a
dependency that is not reachable from Brahmastra (report those upstream, though
we welcome the heads-up); missing hardening headers with no demonstrated impact.

## How releases are protected

Every release image is built by `.github/workflows/release.yml` from a commit
on `main`, signed with Sigstore keyless signing, and shipped with SLSA build
provenance and an SBOM. Servers verify the signatures before installing
anything (`docs/OPERATIONS.md`, *Signed releases*). Continuous scanning:
gitleaks over the full history, pip-audit, pnpm audit, Trivy and CodeQL, on
every pull request and nightly (`.github/workflows/security.yml`).
