"""
Refuse to start a production install with unsafe settings.

Every misconfiguration this project has shipped failed OPEN: workspace
isolation that leaked when a filter was forgotten, CORS left at "*", an API
that served the graph to anyone once a key went missing. A checklist in a doc
does not stop the next one; a process that will not start does. So this runs on
EVERY startup (the API's lifespan, the scheduler, the backup job), not only at
setup -- an edited .env or a half-applied upgrade is checked just the same.

It applies only when BRAHMASTRA_ENV=production. Development keeps its
conveniences (anonymous mode, default passwords on a laptop's compose stack),
and production cannot have them by accident.

REFUSED in production (the process exits with the reasons, so `docker compose
up` and deploy/upgrade.sh see a failed container rather than a quietly unsafe
one):

  * BRAHMASTRA_ALLOW_ANONYMOUS set              -- the API would be open
  * a secret that is empty, a known default, or short:
      POSTGRES_PASSWORD, NEO4J_PASSWORD, and when set BRAHMASTRA_API_KEY,
      BRAHMASTRA_SETUP_TOKEN, RESTIC_PASSWORD
  * CORS_ORIGINS containing "*"
  * notes not in Postgres (NOTE_BACKEND)        -- a SQLite system of record
                                                   is one file on one disk

WARNED (logged, start continues): no admin account yet (setup creates one
after the stack is up, so refusing would make setup impossible), PUBLIC_URL
not https, BRAHMASTRA_KEY_ADMIN on.

    python -m brahmastra.preflight            # check the current environment
"""

from __future__ import annotations

import os

# Values that appear in this repository's compose files, docs and common
# examples. A production secret equal to any of them is as good as public.
KNOWN_DEFAULTS = frozenset({
    "", "brahmastra", "brahmastra-test", "postgres", "neo4j", "password", "changeme",
    "change-me", "secret", "admin", "test", "example", "default", "letmein", "12345678",
})
MIN_SECRET = {"POSTGRES_PASSWORD": 16, "NEO4J_PASSWORD": 16, "BRAHMASTRA_API_KEY": 32,
              "BRAHMASTRA_SETUP_TOKEN": 24, "RESTIC_PASSWORD": 24}
ALWAYS_REQUIRED = ("POSTGRES_PASSWORD", "NEO4J_PASSWORD")


class UnsafeConfiguration(SystemExit):
    pass


def is_production() -> bool:
    return os.environ.get("BRAHMASTRA_ENV", "").strip().lower() in {"production", "prod"}


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def problems(env: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """(refusals, warnings) for the given environment (default: os.environ)."""
    env = dict(os.environ if env is None else env)
    get = lambda k: (env.get(k) or "").strip()          # noqa: E731
    refuse: list[str] = []
    warn: list[str] = []

    if get("BRAHMASTRA_ALLOW_ANONYMOUS").lower() in {"1", "true", "yes", "on"}:
        refuse.append("BRAHMASTRA_ALLOW_ANONYMOUS is set: the API would serve anyone. Remove it.")

    for name, minimum in MIN_SECRET.items():
        value = get(name)
        if not value and name not in ALWAYS_REQUIRED:
            continue
        if not value:
            refuse.append(f"{name} is empty.")
        elif value.lower() in KNOWN_DEFAULTS:
            refuse.append(f"{name} is a well-known default value. Generate one (deploy/setup.sh does).")
        elif len(value) < minimum:
            refuse.append(f"{name} is shorter than {minimum} characters.")

    if "*" in [o.strip() for o in get("CORS_ORIGINS").split(",")]:
        refuse.append('CORS_ORIGINS contains "*": any website could call the API as a signed-in user.')

    if get("NOTE_BACKEND").lower() != "postgres":
        refuse.append("NOTE_BACKEND must be postgres in production: notes are the system of record.")

    public = get("PUBLIC_URL")
    if public and not public.startswith("https://"):
        warn.append(f"PUBLIC_URL ({public}) is not https: sessions and tokens would cross the network in clear.")
    if get("BRAHMASTRA_KEY_ADMIN"):
        warn.append("BRAHMASTRA_KEY_ADMIN is on: admins can add LLM keys from the dashboard.")
    return refuse, warn


def check(*, exit_on_failure: bool = True, check_admin: bool = True) -> list[str]:
    """Run at startup. Exits the process on a refusal in production; returns warnings."""
    if not is_production():
        return []
    refuse, warn = problems()
    if check_admin:
        try:
            from brahmastra.accounts import store

            if store().user_count() == 0:
                warn.append("No accounts exist yet. Create the first admin: "
                            "python -m brahmastra.accounts create-admin --email you@example.com")
        except Exception:                                        # noqa: BLE001
            pass                    # the store being down is readiness's job to report
    import logging

    from brahmastra import logs

    logs.configure()                    # JSON in production, like every other line
    log = logging.getLogger("brahmastra.preflight")
    for w in warn:
        log.warning(w, extra={"event": "preflight", "outcome": "warning"})
    if refuse:
        msg = "Refusing to start: unsafe production configuration. " + " | ".join(refuse)
        log.error(msg, extra={"event": "preflight", "outcome": "refused"})
        if exit_on_failure:
            raise UnsafeConfiguration(78)       # EX_CONFIG
    return warn


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m brahmastra.preflight")
    ap.add_argument("--enforce", action="store_true",
                    help="the container entrypoint: exit 78 on a refusal, print nothing on success")
    args = ap.parse_args(argv)
    if args.enforce:
        try:
            check(check_admin=False)
        except UnsafeConfiguration as e:
            return int(e.code)
        return 0
    refuse, warn = problems()
    mode = "production" if is_production() else "development (checks apply only when BRAHMASTRA_ENV=production)"
    print(f"mode: {mode}")
    for r in refuse:
        print(f"REFUSE  {r}")
    for w in warn:
        print(f"WARN    {w}")
    if not refuse and not warn:
        print("ok")
    return 1 if (refuse and is_production()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
