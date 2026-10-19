"""
Plans: which features a person may use, beside -- never instead of -- their role.

Phase A of plans: features only. Limits are stored but counted in later
phases. Most tests here are refusals, and each one pairs a plan with a role
that WOULD allow the request, because the point of a plan is to narrow a role.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

PW = "correct horse battery"
NOTE = {"id": "n-plan-1", "title": "t", "content": "Sarah works at Acme."}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "plans.db"))
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RATE_LIMITS", "off")
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    from brahmastra import auth
    auth.reset_for_tests()
    return TestClient(main.app, raise_server_exceptions=False)


def _store():
    from brahmastra import accounts
    return accounts.store()


def _login(client, email):
    r = client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------------------------------------------------------- who gets which plan

def test_new_people_start_on_their_roles_plan_unless_the_admin_picks_one(client):
    s = _store()
    assert s.create_user("v@example.com", PW, role="viewer")["plan"] == "reader"
    assert s.create_user("m@example.com", PW, role="member")["plan"] == "contributor"
    assert s.create_user("a@example.com", PW, role="admin")["plan"] == "unlimited"
    # An admin may start someone smaller than their role allows...
    assert s.create_user("r@example.com", PW, role="member", plan="reader")["plan"] == "reader"
    # ...but not on a plan that does not exist.
    from brahmastra.accounts import AccountError
    with pytest.raises(AccountError, match="No such plan"):
        s.create_user("x@example.com", PW, role="member", plan="platinum")


def test_introducing_plans_takes_nobodys_access_away(client):
    """An install upgraded to plans already has users with no plan row. Each
    gets the plan matching what their role allowed the day before."""
    s = _store()
    viewer = s.create_user("v@example.com", PW, role="viewer")
    member = s.create_user("m@example.com", PW, role="member")
    admin = s.create_user("a@example.com", PW, role="admin")
    # Back to the day before plans existed.
    s._q("DELETE FROM user_plans")
    s._q("DELETE FROM plans")

    s.ensure_plans()

    assert {p["id"] for p in s.list_plans()} == {"reader", "contributor", "power", "unlimited"}
    assert s.get_user(viewer["id"])["plan"] == "reader"
    assert s.get_user(member["id"])["plan"] == "contributor"
    assert s.get_user(admin["id"])["plan"] == "unlimited"
    assert {r["assigned_by"] for r in s._q("SELECT assigned_by FROM user_plans", rows=True)} == {"rollout"}


def test_seeding_twice_changes_nothing(client):
    s = _store()
    s.create_user("m@example.com", PW, role="member")
    before = s._q("SELECT COUNT(*) AS n FROM plans", one=True)["n"]
    s.ensure_plans()
    s.ensure_plans()
    assert s._q("SELECT COUNT(*) AS n FROM plans", one=True)["n"] == before == 4


# ---------------------------------------------------------------- the plan narrows the role

def test_a_member_on_the_reader_plan_can_read_and_ask_but_not_write(client):
    """The role allows writing; the plan does not. The plan wins, and says so."""
    _store().create_user("r@example.com", PW, role="member", plan="reader", workspaces=["default"])
    h = _login(client, "r@example.com")
    assert client.get("/notes", headers=h).status_code == 200
    assert client.get("/graph/stats", headers=h).status_code == 200
    r = client.post("/notes", json=NOTE, headers=h)
    assert r.status_code == 403
    assert r.json()["code"] == "plan_feature" and r.json()["feature"] == "add_notes"
    assert "not in your plan" in r.json()["detail"]


def test_meetings_need_the_power_plan(client):
    _store().create_user("c@example.com", PW, role="member", workspaces=["default"])
    h = _login(client, "c@example.com")
    assert client.get("/ingest/transcripts", headers=h).status_code == 200      # reading results
    r = client.post("/ingest/transcripts", json={"title": "x", "text": "A: hi"}, headers=h)
    assert r.status_code == 403 and r.json()["feature"] == "meetings"


def test_a_plan_never_widens_a_role(client):
    """A viewer on the largest plan is still a viewer."""
    _store().create_user("v@example.com", PW, role="viewer", plan="unlimited", workspaces=["default"])
    h = _login(client, "v@example.com")
    r = client.post("/notes", json=NOTE, headers=h)
    assert r.status_code == 403 and "role" in r.json()["detail"]


def test_a_plan_change_applies_at_the_next_request(client):
    s = _store()
    s.create_user("boss@example.com", PW, role="admin")
    m = s.create_user("m@example.com", PW, role="member", workspaces=["default"])
    boss, h = _login(client, "boss@example.com"), _login(client, "m@example.com")
    assert client.post("/notes", json=NOTE, headers=h).status_code == 201

    r = client.put(f"/auth/users/{m['id']}/plan", json={"plan_id": "reader"}, headers=boss)
    assert r.status_code == 200 and r.json()["plan"] == "reader"
    assert client.post("/notes", json={**NOTE, "id": "n-plan-2"}, headers=h).status_code == 403
    assert client.get("/auth/me", headers=h).json()["plan"] == "reader"


def test_the_service_key_and_anonymous_mode_are_not_plan_limited(client, monkeypatch):
    from brahmastra.accounts import ANONYMOUS, SERVICE
    assert SERVICE.has("meetings") and ANONYMOUS.has("meetings")


# ---------------------------------------------------------------- assigning plans

def test_nobody_changes_their_own_plan_while_another_admin_exists(client):
    s = _store()
    a = s.create_user("a@example.com", PW, role="admin")
    s.create_user("b@example.com", PW, role="admin")
    h = _login(client, "a@example.com")
    r = client.put(f"/auth/users/{a['id']}/plan", json={"plan_id": "reader"}, headers=h)
    assert r.status_code == 403 and "own plan" in r.json()["detail"]


def test_a_sole_admin_may_change_their_own_plan_and_the_audit_says_so(client):
    s = _store()
    a = s.create_user("a@example.com", PW, role="admin")
    h = _login(client, "a@example.com")
    assert client.put(f"/auth/users/{a['id']}/plan", json={"plan_id": "power"},
                      headers=h).status_code == 200
    entry = next(e for e in s.audit_entries() if e["action"] == "plan.assign")
    assert entry["detail"] == "unlimited -> power (self-assigned: sole admin)"


def test_only_admins_assign_plans_and_only_real_plans(client):
    s = _store()
    s.create_user("a@example.com", PW, role="admin")
    m = s.create_user("m@example.com", PW, role="member", workspaces=["default"])
    member, admin = _login(client, "m@example.com"), _login(client, "a@example.com")
    assert client.put(f"/auth/users/{m['id']}/plan", json={"plan_id": "power"},
                      headers=member).status_code == 403
    assert client.put(f"/auth/users/{m['id']}/plan", json={"plan_id": "platinum"},
                      headers=admin).status_code == 404
    plans = client.get("/auth/plans", headers=member).json()
    assert [p["id"] for p in plans["plans"]] == ["reader", "contributor", "power", "unlimited"]
    assert "meetings" in plans["features"]


# ---------------------------------------------------------------- coverage, so it cannot rot

# Routes deliberately outside plans: accounts, admin work, the pipeline and
# health. Roles already decide them, and none spends tokens a plan should meter
# on a person's behalf.
NOT_A_PLAN_FEATURE = ("/auth", "/diagnostics", "/pipeline", "/health")


def test_every_route_is_either_a_plan_feature_or_deliberately_not(client):
    """A new route that spends tokens must not slip past plans unnoticed: it
    either maps to a feature or is listed above as deliberately exempt."""
    import main
    from brahmastra.plans import route_feature

    for path, ops in main.app.openapi()["paths"].items():
        for method in ops:
            if path.startswith(NOT_A_PLAN_FEATURE) or path in ("/workspaces", "/workspaces/{workspace_id}"):
                continue
            assert route_feature(method.upper(), path.replace("{", "").replace("}", "")) is not None, \
                f"{method.upper()} {path} has no plan feature"


def test_every_plan_names_only_real_features_and_limits():
    from brahmastra.plans import DEFAULT_PLANS, validate
    for plan in DEFAULT_PLANS:
        validate(plan["features"], plan["limits"])
    with pytest.raises(ValueError, match="Unknown feature"):
        validate(["teleport"], {})
    with pytest.raises(ValueError, match="whole number"):
        validate([], {"questions_month": -1})
