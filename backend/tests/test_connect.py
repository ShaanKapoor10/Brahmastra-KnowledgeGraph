"""
GET /auth/me/connect: what the dashboard's "Connect an agent" screen shows.

The address is the part that is easy to get wrong. Behind Caddy the API is
PUBLIC_URL/v1 and remote MCP is PUBLIC_URL/v1/mcp; a backend on its own is
localhost:8001. The dashboard cannot tell which it is in, so the backend says.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

from brahmastra.routers.auth_routes import public_api_url  # noqa: E402

PW = "correct horse battery staple"


@pytest.mark.parametrize("env, expected", [
    ({"PUBLIC_URL": "https://kb.example.com"}, "https://kb.example.com/v1"),
    ({"PUBLIC_URL": "https://kb.example.com/"}, "https://kb.example.com/v1"),
    ({}, "http://localhost:8001"),
    ({"PUBLIC_URL": "https://kb.example.com", "MCP_PUBLIC_URL": "https://mcp.example.com/api/"},
     "https://mcp.example.com/api"),
])
def test_the_address_is_the_one_a_client_outside_reaches(env, expected):
    assert public_api_url(env) == expected


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "connect.db"))
    monkeypatch.setenv("MCP_HTTP", "1")
    monkeypatch.setenv("RATE_LIMITS", "off")
    monkeypatch.setenv("PUBLIC_URL", "https://kb.example.com")
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN",
                "MCP_PUBLIC_URL", "BRAHMASTRA_MARKETPLACE"):
        monkeypatch.delenv(key, raising=False)
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    for ws in ("alpha", "beta"):
        db_mod.create_workspace(ws, ws)
    from brahmastra import accounts, auth, mcp_server
    importlib.reload(mcp_server)
    import main
    importlib.reload(main)
    auth.reset_for_tests()
    return main.app, accounts.store()


def _get(client, token):
    return client.get("/auth/me/connect", headers={"Authorization": f"Bearer {token}"})


def test_a_member_sees_the_address_and_only_their_own_workspaces(app):
    application, store = app
    mia = store.create_user("mia@example.com", PW, role="member", workspaces=["alpha"])
    token = store.create_token(mia["id"], "laptop")["token"]
    with TestClient(application) as client:
        out = _get(client, token).json()
    assert out["mcp_url"] == "https://kb.example.com/v1/mcp"
    assert out["server_url"] == "https://kb.example.com/v1"
    assert out["workspaces"] == ["alpha"] and out["workspace"] == "alpha"
    assert out["enabled"] is True and out["needs_token"] is True
    assert out["plugin"] == "brahmastra@brahmastra"
    assert token not in str(out), "the secret is never sent back"


def test_a_plan_without_mcp_is_told_so(app):
    """The screen says so up front, instead of a token that then fails."""
    application, store = app
    store.update_plan("reader", actor="test", features=["ask", "browse", "graph"])
    rita = store.create_user("rita@example.com", PW, role="member", workspaces=["alpha"], plan="reader")
    token = store.create_token(rita["id"], "laptop")["token"]
    with TestClient(application) as client:
        out = _get(client, token).json()
    assert out["allowed"] is False


def test_an_organisation_points_installs_at_its_own_repository(app, monkeypatch):
    application, store = app
    monkeypatch.setenv("BRAHMASTRA_MARKETPLACE", "acme/brahmastra")
    admin = store.create_user("admin@example.com", PW, role="admin")
    token = store.create_token(admin["id"], "laptop")["token"]
    with TestClient(application) as client:
        out = _get(client, token).json()
    assert out["marketplace"] == "acme/brahmastra"
    assert set(out["workspaces"]) >= {"alpha", "beta"}


def test_development_mode_needs_no_token(app, monkeypatch):
    application, _ = app
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    from brahmastra import auth
    auth.reset_for_tests()
    with TestClient(application) as client:
        out = client.get("/auth/me/connect").json()
    assert out["needs_token"] is False
