"""
The production startup check (brahmastra/preflight.py) refuses unsafe settings
on every start, so a bad .env stops the container instead of serving the graph.
"""
from __future__ import annotations

import pytest

from brahmastra import preflight

STRONG = "x7Qp2LmZ9vR4tY8wK3nB6cJ1"


def _env(**over):
    base = {"BRAHMASTRA_ENV": "production", "NOTE_BACKEND": "postgres",
            "POSTGRES_PASSWORD": STRONG, "NEO4J_PASSWORD": STRONG + "n", "PUBLIC_URL": "https://kb.example.com"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_a_sound_production_config_passes():
    assert preflight.problems(_env()) == ([], [])


@pytest.mark.parametrize("over, fragment", [
    (dict(BRAHMASTRA_ALLOW_ANONYMOUS="1"), "ALLOW_ANONYMOUS"),
    (dict(POSTGRES_PASSWORD=""), "POSTGRES_PASSWORD is empty"),
    (dict(POSTGRES_PASSWORD="brahmastra"), "well-known default"),
    (dict(NEO4J_PASSWORD="short-pass"), "shorter than 16"),
    (dict(BRAHMASTRA_API_KEY="changeme"), "well-known default"),
    (dict(BRAHMASTRA_SETUP_TOKEN="abc"), "shorter than 24"),
    (dict(RESTIC_PASSWORD="password"), "well-known default"),
    (dict(CORS_ORIGINS="https://a.example, *"), "CORS_ORIGINS"),
    (dict(NOTE_BACKEND="sqlite"), "NOTE_BACKEND"),
])
def test_unsafe_settings_are_refused(over, fragment):
    refuse, _ = preflight.problems(_env(**over))
    assert any(fragment in r for r in refuse), refuse


def test_plain_http_is_a_warning_not_a_refusal():
    refuse, warn = preflight.problems(_env(PUBLIC_URL="http://kb.example.com"))
    assert refuse == [] and any("not https" in w for w in warn)


def test_check_exits_in_production_and_does_nothing_in_development(monkeypatch):
    # NOTE_BACKEND stays out of the real environment: conftest refuses tests that
    # point it at Postgres. Leaving it unset is itself a refusal, which is enough.
    for k, v in _env(POSTGRES_PASSWORD="postgres", NOTE_BACKEND=None).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as e:
        preflight.check(check_admin=False)
    assert e.value.code == 78
    monkeypatch.setenv("BRAHMASTRA_ENV", "development")
    assert preflight.check(check_admin=False) == []
