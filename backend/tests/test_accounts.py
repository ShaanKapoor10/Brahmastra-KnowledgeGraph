"""
User accounts, sessions, API tokens, roles and workspace membership.

The point of accounts is that the dashboard stops handing every visitor the
shared API key, so most tests here are about refusals: a viewer that cannot
write, a member that cannot open another team's workspace, a revoked token, a
second "first admin", an anonymous mode that switches itself off.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

PW = "correct horse battery"


@pytest.fixture
def make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "accounts.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    def build(**env: str | None) -> TestClient:
        import main
        importlib.reload(main)
        for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        return TestClient(main.app, raise_server_exceptions=False)

    return build


def _store():
    from brahmastra import accounts
    return accounts.store()


def _login(client, email, password=PW):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_unconfigured_still_fails_closed_and_says_how_to_create_an_admin(make_app):
    client = make_app()
    r = client.get("/notes")
    assert r.status_code == 503
    assert "create-admin" in r.json()["detail"]
    assert client.get("/auth/status").json()["setup_required"] is True


def test_anonymous_mode_switches_itself_off_once_a_user_exists(make_app):
    client = make_app(BRAHMASTRA_ALLOW_ANONYMOUS="1")
    assert client.get("/notes").status_code == 200
    _store().create_user("admin@example.com", PW, role="admin")
    from brahmastra import auth
    auth.reset_for_tests()
    assert client.get("/notes").status_code == 401


def test_login_me_and_logout(make_app):
    client = make_app()
    _store().create_user("ana@example.com", PW, role="member", workspaces=["default"])
    h = _login(client, "ANA@example.com ")                      # email is normalised
    me = client.get("/auth/me", headers=h).json()
    assert me["email"] == "ana@example.com" and me["role"] == "member" and me["kind"] == "session"
    assert client.post("/auth/logout", headers=h).status_code == 204
    assert client.get("/auth/me", headers=h).status_code == 401


def test_a_wrong_email_and_a_wrong_password_get_the_same_answer(make_app):
    client = make_app()
    _store().create_user("ana@example.com", PW)
    a = client.post("/auth/login", json={"email": "nobody@example.com", "password": PW})
    b = client.post("/auth/login", json={"email": "ana@example.com", "password": "wrong password!"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_repeated_failures_lock_the_login(make_app):
    client = make_app()
    _store().create_user("ana@example.com", PW)
    for _ in range(5):
        client.post("/auth/login", json={"email": "ana@example.com", "password": "not it at all"})
    r = client.post("/auth/login", json={"email": "ana@example.com", "password": PW})
    assert r.status_code == 429


def test_roles_viewer_reads_member_writes_admin_manages(make_app):
    client = make_app()
    s = _store()
    s.create_user("admin@example.com", PW, role="admin")
    s.create_user("vic@example.com", PW, role="viewer", workspaces=["default"])
    s.create_user("mo@example.com", PW, role="member", workspaces=["default"])
    viewer, member, admin = (_login(client, e) for e in
                             ("vic@example.com", "mo@example.com", "admin@example.com"))
    note = {"id": "n-roles-1", "title": "t", "content": "Sarah works at Acme."}

    assert client.get("/notes", headers=viewer).status_code == 200
    assert client.post("/notes", json=note, headers=viewer).status_code == 403
    assert client.post("/notes", json=note, headers=member).status_code == 201
    assert client.get("/auth/users", headers=member).status_code == 403
    assert client.get("/auth/users", headers=admin).status_code == 200
    assert client.post("/workspaces", json={"name": "x"}, headers=member).status_code == 403


def test_a_member_sees_only_their_workspaces(make_app):
    client = make_app()
    from brahmastra import db
    db.create_workspace(id="alpha", name="Alpha")
    db.create_workspace(id="beta", name="Beta")
    _store().create_user("mo@example.com", PW, role="member", workspaces=["alpha"])
    h = _login(client, "mo@example.com")

    assert client.get("/notes?workspace=alpha", headers=h).status_code == 200
    r = client.get("/notes?workspace=beta", headers=h)
    assert r.status_code == 403 and "beta" in r.json()["detail"]
    assert client.get("/notes", headers={**h, "X-Brahmastra-Workspace": "beta"}).status_code == 403
    listed = {w["id"] for w in client.get("/workspaces", headers=h).json()["workspaces"]}
    assert listed == {"alpha"}
    assert client.get("/workspaces/beta", headers=h).status_code == 404   # not even its existence


def test_api_tokens_work_until_revoked_and_are_stored_only_as_hashes(make_app):
    client = make_app()
    u = _store().create_user("mo@example.com", PW, role="member", workspaces=["default"])
    h = _login(client, "mo@example.com")
    made = client.post("/auth/tokens", json={"name": "laptop MCP"}, headers=h).json()
    secret = made["token"]
    assert secret.startswith("bra_")
    listed = client.get("/auth/tokens", headers=h).json()["tokens"]
    assert listed[0]["name"] == "laptop MCP" and "token" not in listed[0]

    t = {"Authorization": f"Bearer {secret}"}
    assert client.get("/notes", headers=t).status_code == 200
    assert client.get("/notes", headers={"X-API-Key": secret}).status_code == 200

    rows = _store()._q("SELECT token_hash FROM api_tokens WHERE user_id = ?", (u["id"],), rows=True)
    assert secret not in str(rows)
    assert client.delete(f"/auth/tokens/{made['id']}", headers=h).status_code == 204
    assert client.get("/notes", headers=t).status_code == 401


def test_the_legacy_shared_key_still_works_as_a_service_identity(make_app):
    client = make_app(BRAHMASTRA_API_KEY="legacy-key-value")
    r = client.get("/auth/me", headers={"Authorization": "Bearer legacy-key-value"})
    assert r.status_code == 200 and r.json()["kind"] == "service" and r.json()["role"] == "admin"
    assert client.get("/notes", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_passwords_are_hashed_and_a_new_one_ends_every_session(make_app):
    client = make_app()
    s = _store()
    u = s.create_user("ana@example.com", PW, role="member", workspaces=["default"])
    row = s._q("SELECT password_hash FROM users WHERE id = ?", (u["id"],), one=True)
    assert row["password_hash"].startswith("scrypt$") and PW not in row["password_hash"]
    h = _login(client, "ana@example.com")
    r = client.post("/auth/password", json={"current_password": PW, "new_password": "another long phrase"},
                    headers=h)
    assert r.status_code == 204
    assert client.get("/auth/me", headers=h).status_code == 401
    _login(client, "ana@example.com", "another long phrase")


def test_weak_passwords_are_refused(make_app):
    from brahmastra.accounts import AccountError
    with pytest.raises(AccountError):
        _store().create_user("a@example.com", "short")
    with pytest.raises(AccountError):
        _store().create_user("a@example.com", "aaaaaaaaaaaaaa")


def test_the_last_admin_cannot_be_demoted_disabled_or_deleted(make_app):
    make_app()
    from brahmastra.accounts import AccountError
    s = _store()
    a = s.create_user("admin@example.com", PW, role="admin")
    for change in (dict(role="member"), dict(disabled=True)):
        with pytest.raises(AccountError, match="last active admin"):
            s.update_user(a["id"], **change)
    with pytest.raises(AccountError, match="last active admin"):
        s.delete_user(a["id"])


def test_a_disabled_user_loses_access_at_once(make_app):
    client = make_app()
    s = _store()
    s.create_user("admin@example.com", PW, role="admin")
    u = s.create_user("mo@example.com", PW, role="member", workspaces=["default"])
    h = _login(client, "mo@example.com")
    s.update_user(u["id"], disabled=True)
    assert client.get("/notes", headers=h).status_code == 401
    assert client.post("/auth/login", json={"email": "mo@example.com", "password": PW}).status_code == 401


def test_http_setup_needs_the_setup_token_and_works_only_once(make_app):
    client = make_app()
    body = {"email": "first@example.com", "password": PW, "setup_token": "x"}
    assert client.post("/auth/setup", json=body).status_code == 404          # not enabled
    client = make_app(BRAHMASTRA_SETUP_TOKEN="one-time-setup-token")
    assert client.post("/auth/setup", json=body).status_code == 403          # wrong token
    ok = client.post("/auth/setup", json={**body, "setup_token": "one-time-setup-token"})
    assert ok.status_code == 201 and ok.json()["user"]["role"] == "admin"
    again = client.post("/auth/setup", json={**body, "email": "second@example.com",
                                              "setup_token": "one-time-setup-token"})
    assert again.status_code == 404


def test_account_events_are_audited(make_app):
    client = make_app()
    _store().create_user("admin@example.com", PW, role="admin")
    client.post("/auth/login", json={"email": "admin@example.com", "password": "wrong password!"})
    h = _login(client, "admin@example.com")
    actions = [e["action"] for e in client.get("/auth/audit", headers=h).json()["entries"]]
    assert {"user.create", "login.failed", "login"} <= set(actions)


def test_remote_mcp_is_admin_only_until_tools_are_scoped_per_user():
    from brahmastra.auth import required_role
    assert required_role("POST", "/mcp") == "admin"
    assert required_role("GET", "/notes") == "viewer"
    assert required_role("POST", "/ask") == "viewer"
    assert required_role("POST", "/notes") == "member"
    assert required_role("DELETE", "/workspaces/x") == "admin"
    assert required_role("GET", "/auth/me") is None


def test_ask_is_rate_limited_per_user(make_app, monkeypatch):
    client = make_app(RATE_LIMITS="ask=2/m")
    _store().create_user("vic@example.com", PW, role="viewer", workspaces=["default"])
    h = _login(client, "vic@example.com")
    from brahmastra import rag
    monkeypatch.setattr(rag, "answer_question", lambda *a, **k: {"answer": "x", "mode": "local"})
    codes = [client.post("/ask", json={"question": "q"}, headers=h).status_code for _ in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429


def test_responses_carry_security_headers_and_no_open_cors(make_app):
    client = make_app(BRAHMASTRA_ALLOW_ANONYMOUS="1")
    r = client.get("/notes", headers={"Origin": "https://evil.example"})
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers


def test_the_uptime_check_is_open_and_says_nothing_but_up_or_down(make_app):
    client = make_app()                        # unconfigured: everything else is 503
    r = client.get("/health/uptime")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_the_cli_can_create_an_admin_from_stdin_for_automation(make_app, monkeypatch, capsys):
    make_app()
    import io
    from brahmastra import accounts
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert accounts.main(["create-admin", "--email", "ops@example.com", "--password-stdin"]) == 0
    assert PW not in capsys.readouterr().out
    assert _store().find_user("ops@example.com")["role"] == "admin"
