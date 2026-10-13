"""
The ONE Postgres a test may talk to, and how to start it.

Integration tests that need a real Postgres run only when
BRAHMASTRA_TEST_POSTGRES_DSN names a database, and only if its name contains
"test". They never fall back to the application's own settings.

They used to. tests/test_postgres_store.py resolved its DSN when the module was
imported, and importing `brahmastra.stores` reads backend/.env -- so on a
developer machine the "integration" tests ran against the deployed Postgres.
Two /health tests built the readiness check there too, and created the account
tables in production. Nothing was lost (every test cleaned up its throwaway
workspace), but "nothing was lost" is luck, not isolation.

A throwaway database, gone when the container stops:

    docker compose -f tests/postgres-test.compose.yml up -d
    set BRAHMASTRA_TEST_POSTGRES_DSN=postgresql://brahmastra:brahmastra-test@localhost:5439/brahmastra_test
    python -m pytest tests/ -q
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

ENV_VAR = "BRAHMASTRA_TEST_POSTGRES_DSN"


class UnsafeTestDatabase(RuntimeError):
    pass


def _target(dsn: str) -> tuple[str, int, str]:
    u = urlparse(dsn)
    return (u.hostname or "localhost").lower(), int(u.port or 5432), (u.path or "/").lstrip("/")


def configured_dsn() -> str | None:
    """The test database, validated, or None when integration tests should skip."""
    dsn = os.environ.get(ENV_VAR, "").strip()
    if not dsn:
        return None
    _, _, name = _target(dsn)
    if "test" not in name.lower():
        raise UnsafeTestDatabase(
            f"{ENV_VAR} names the database {name!r}. Test databases must have 'test' in their "
            "name, so an application database can never be pointed at by mistake.")
    return dsn


def reachable_dsn() -> str | None:
    """The configured test DSN if something answers on it."""
    dsn = configured_dsn()
    if dsn is None:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3):
            return dsn
    except Exception:
        return None


def is_allowed(conninfo: str | None, kwargs: dict) -> bool:
    """Whether a psycopg.connect() call targets the test database."""
    allowed = configured_dsn()
    if not allowed:
        return False
    if conninfo and "://" in conninfo:
        return _target(conninfo) == _target(allowed)
    host, port, name = _target(allowed)
    return (str(kwargs.get("host", "")).lower() == host and int(kwargs.get("port", 5432)) == port
            and kwargs.get("dbname") == name)
