"""
Upgrades (phase D of plans): people ask, admins decide, everything is recorded.

The rules under test: a request changes nothing until an admin approves it;
nobody approves their own while another admin could; a top-up raises one
limit for one day or month and then is gone; a time-limited plan returns the
person to the plan it replaced; and editing a plan changes it for everyone on
it, validated and audited.
"""
from __future__ import annotations

import importlib
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

PW = "correct horse battery"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "requests.db"))
    monkeypatch.setenv("RATE_LIMITS", "off")
    monkeypatch.setenv("TZ", "UTC")
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    from brahmastra import auth, usage
    auth.reset_for_tests()
    usage.reset()
    return TestClient(main.app, raise_server_exceptions=False)


def _store():
    from brahmastra import accounts
    return accounts.store()


def _login(client, email):
    r = client.post("/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def team(client):
    s = _store()
    boss = s.create_user("boss@example.com", PW, role="admin")
    other = s.create_user("other@example.com", PW, role="admin")
    mia = s.create_user("mia@example.com", PW, role="member", workspaces=["default"])
    return {"boss": (boss, _login(client, "boss@example.com")),
            "other": (other, _login(client, "other@example.com")),
            "mia": (mia, _login(client, "mia@example.com"))}


def _ask(client, h, **body):
    return client.post("/auth/me/requests", json=body, headers=h)


# ---------------------------------------------------------------- the round trip

def test_a_plan_request_changes_nothing_until_an_admin_approves_it(client, team):
    mia, h = team["mia"]
    r = _ask(client, h, kind="plan", plan_id="power", reason="I run the weekly team meeting")
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert client.get("/auth/me", headers=h).json()["plan"] == "contributor"

    boss_h = team["boss"][1]
    queue = client.get("/auth/requests?status=pending", headers=boss_h).json()
    assert queue["pending"] == 1
    entry = queue["requests"][0]
    assert entry["email"] == "mia@example.com" and entry["current_plan"] == "contributor"
    assert {u["limit_key"] for u in entry["usage"]} >= {"questions_month", "notes_month", "tokens_month"}

    r = client.post(f"/auth/requests/{entry['id']}/approve", json={"note": "for the pilot"}, headers=boss_h)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.get("/auth/me", headers=h).json()["plan"] == "power"
    mine = client.get("/auth/me/requests", headers=h).json()["requests"][0]
    assert (mine["status"], mine["decision_note"]) == ("approved", "for the pilot")
    actions = [e["action"] for e in _store().audit_entries()]
    assert {"plan.request", "plan.request.approve", "plan.assign"} <= set(actions)


def test_a_declined_request_says_why_and_changes_nothing(client, team):
    _, h = team["mia"]
    rid = _ask(client, h, kind="plan", plan_id="power", reason="just curious").json()["id"]
    r = client.post(f"/auth/requests/{rid}/decline", json={"note": "Ask your lead first"}, headers=team["boss"][1])
    assert r.json()["status"] == "declined"
    assert client.get("/auth/me", headers=h).json()["plan"] == "contributor"
    assert client.get("/auth/me/requests", headers=h).json()["requests"][0]["decision_note"] == "Ask your lead first"
    # Decided once is decided.
    assert client.post(f"/auth/requests/{rid}/approve", json={}, headers=team["boss"][1]).status_code == 400


def test_one_request_of_a_kind_waits_at_a_time_and_its_author_can_cancel_it(client, team):
    _, h = team["mia"]
    rid = _ask(client, h, kind="plan", plan_id="power", reason="meetings").json()["id"]
    assert _ask(client, h, kind="plan", plan_id="reader", reason="changed my mind").status_code == 400
    assert _ask(client, h, kind="plan", plan_id="contributor", reason="same").status_code == 400  # already on it
    # Someone else cannot cancel it; its author can.
    assert client.delete(f"/auth/me/requests/{rid}", headers=team["boss"][1]).status_code == 404
    assert client.delete(f"/auth/me/requests/{rid}", headers=h).status_code == 204
    assert _ask(client, h, kind="plan", plan_id="reader", reason="changed my mind").status_code == 201


def test_nobody_approves_their_own_request_while_another_admin_exists(client, team):
    boss, h = team["boss"]
    rid = _ask(client, h, kind="topup", limit_key="tokens_month", amount=1000, reason="big import").json()["id"]
    r = client.post(f"/auth/requests/{rid}/approve", json={}, headers=h)
    assert r.status_code == 403 and "own request" in r.json()["detail"]
    assert client.post(f"/auth/requests/{rid}/approve", json={}, headers=team["other"][1]).status_code == 200


def test_members_cannot_see_or_decide_the_queue(client, team):
    _, h = team["mia"]
    assert client.get("/auth/requests", headers=h).status_code == 403
    rid = _ask(client, h, kind="plan", plan_id="power", reason="meetings").json()["id"]
    assert client.post(f"/auth/requests/{rid}/approve", json={}, headers=h).status_code == 403


# ---------------------------------------------------------------- top-ups and end dates

def test_a_top_up_raises_one_limit_for_this_month_only(client, team, monkeypatch):
    from brahmastra import usage
    mia, h = team["mia"]
    s = _store()
    before = s.effective_limits(mia["id"], "member")["questions_month"]
    rid = _ask(client, h, kind="topup", limit_key="questions_month", amount=100, reason="audit season").json()["id"]
    client.post(f"/auth/requests/{rid}/approve", json={}, headers=team["boss"][1])
    assert s.effective_limits(mia["id"], "member")["questions_month"] == before + 100
    assert s.effective_limits(mia["id"], "member")["notes_month"] == s.get_plan("contributor")["limits"]["notes_month"]
    # Next month, the plan's own limit again.
    from datetime import datetime
    monkeypatch.setattr(usage, "_tz_now", lambda: datetime(2099, 1, 15, tzinfo=usage._tz()))
    assert s.effective_limits(mia["id"], "member")["questions_month"] == before


def test_a_time_limited_plan_returns_the_person_to_the_plan_it_replaced(client, team, monkeypatch):
    mia, h = team["mia"]
    rid = _ask(client, h, kind="plan", plan_id="power", reason="two-week project").json()["id"]
    ends = time.time() + 14 * 86400
    client.post(f"/auth/requests/{rid}/approve", json={"expires_at": ends}, headers=team["boss"][1])
    assert client.get("/auth/me", headers=h).json()["plan"] == "power"
    assert _store().get_user(mia["id"])["plan_expires_at"] == pytest.approx(ends)

    import brahmastra.accounts as accounts_mod
    real = time.time
    monkeypatch.setattr(accounts_mod.time, "time", lambda: real() + 15 * 86400)
    assert _store().get_user(mia["id"])["plan"] == "contributor"
    assert any(e["action"] == "plan.expire" for e in _store().audit_entries())


def test_an_end_date_in_the_past_is_refused(client, team):
    mia, _ = team["mia"]
    r = client.put(f"/auth/users/{mia['id']}/plan", json={"plan_id": "power", "expires_at": time.time() - 60},
                   headers=team["boss"][1])
    assert r.status_code == 400 and "future" in r.json()["detail"]


# ---------------------------------------------------------------- editing a plan

def test_editing_a_plan_changes_it_for_everyone_on_it_and_is_audited(client, team):
    _, h = team["mia"]
    boss_h = team["boss"][1]
    r = client.put("/auth/plans/contributor", json={"limits": {"notes_day": 2}, "features": [
        "ask", "browse", "graph", "mcp", "cross_workspace", "add_notes", "documents", "meetings"]}, headers=boss_h)
    assert r.status_code == 200
    assert r.json()["limits"]["notes_day"] == 2 and "meetings" in r.json()["features"]
    assert "meetings" in client.get("/auth/me", headers=h).json()["features"]
    entry = next(e for e in _store().audit_entries() if e["action"] == "plan.edit")
    assert "+meetings" in entry["detail"] and "notes_day: 50 -> 2" in entry["detail"]


def test_a_plan_edit_is_validated_and_only_admins_may_make_one(client, team):
    boss_h, mia_h = team["boss"][1], team["mia"][1]
    assert client.put("/auth/plans/reader", json={"features": ["teleport"]}, headers=boss_h).status_code == 400
    assert client.put("/auth/plans/reader", json={"limits": {"questions_day": -5}}, headers=boss_h).status_code == 400
    assert client.put("/auth/plans/unlimited", json={"features": ["ask"]}, headers=boss_h).status_code == 400
    assert client.put("/auth/plans/reader", json={"limits": {"questions_day": 5}}, headers=mia_h).status_code == 403
    assert client.get("/auth/plans", headers=mia_h).status_code == 200
