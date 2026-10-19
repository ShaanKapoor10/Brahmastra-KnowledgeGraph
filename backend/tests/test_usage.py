"""
Plan limits, counted: questions, notes, words and meeting minutes, per person,
per day and per month (phase B of plans).

The guarantees under test: the N+1th use is refused and the first N are not;
a refused or failed request counts nothing; two requests racing for the last
slot cannot both have it; days and months roll over on their own; and the
count lives in the database, so it holds across workers -- on Postgres too.
"""
from __future__ import annotations

import importlib
import json
import threading
from datetime import datetime, timedelta

import pytest

from brahmastra import usage

PW = "correct horse battery"


# ---------------------------------------------------------------- the counter itself

class _Person:
    """The identity fields usage.charge reads."""

    def __init__(self, user_id: str, role: str = "member"):
        self.user_id, self.role = user_id, role


@pytest.fixture
def counting(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv("TZ", "UTC")
    usage.reset()
    limits = {"questions_month": 5, "questions_day": 3, "notes_month": 4, "notes_day": 4,
              "words_month": 20, "meeting_hours_month": 1}
    monkeypatch.setattr(usage, "_limits_for", lambda identity: limits)
    yield limits
    usage.reset()


def test_the_limit_refuses_exactly_the_one_over(counting):
    p = _Person("u1")
    for _ in range(3):
        usage.charge(p, "questions")
    with pytest.raises(usage.LimitReached) as e:
        usage.charge(p, "questions")
    assert e.value.period == "day" and e.value.limit == 3
    assert "You've used your 3 questions today" in str(e.value)
    # The refused question was not counted.
    assert usage._store().add("u1", "questions", usage.periods()["day"][0], 0) == 3


def test_a_new_day_starts_fresh_but_the_month_remembers(counting, monkeypatch):
    p = _Person("u1")
    day1 = datetime(2026, 10, 19, 12, tzinfo=usage._tz())
    monkeypatch.setattr(usage, "_tz_now", lambda: day1)
    for _ in range(3):
        usage.charge(p, "questions")
    monkeypatch.setattr(usage, "_tz_now", lambda: day1 + timedelta(days=1))
    usage.charge(p, "questions")
    usage.charge(p, "questions")            # five this month: the monthly limit
    with pytest.raises(usage.LimitReached) as e:
        usage.charge(p, "questions")
    assert e.value.period == "month"
    assert "They reset on 1 November" in str(e.value)
    monkeypatch.setattr(usage, "_tz_now", lambda: datetime(2026, 11, 1, 0, 5, tzinfo=usage._tz()))
    usage.charge(p, "questions")            # a new month


def test_a_note_too_long_counts_neither_the_note_nor_its_words(counting):
    p = _Person("u1")
    usage.charge_note(p, "one two three")
    with pytest.raises(usage.LimitReached) as e:
        usage.charge_note(p, " ".join(["w"] * 18))           # 3 + 18 > 20 words
    assert e.value.meter == "words"
    month = usage.periods()["month"][0]
    assert usage._store().add("u1", "notes", month, 0) == 1
    assert usage._store().add("u1", "words", month, 0) == 3


def test_meetings_are_counted_in_minutes_against_hours(counting):
    p = _Person("u1")
    usage.charge(p, "meeting_minutes", usage.meeting_minutes(" ".join(["w"] * 150 * 40)))   # 40 min
    with pytest.raises(usage.LimitReached) as e:
        usage.charge(p, "meeting_minutes", usage.meeting_minutes(" ".join(["w"] * 150 * 30)))
    assert "You've used your 1 meeting hours this month" in str(e.value)


def test_two_requests_cannot_both_take_the_last_slot(counting):
    p, allowed = _Person("u1"), []

    def ask():
        try:
            usage.charge(p, "questions")
            allowed.append(1)
        except usage.LimitReached:
            pass

    threads = [threading.Thread(target=ask) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 3


def test_the_service_key_and_anonymous_mode_are_never_counted(counting):
    from brahmastra.accounts import ANONYMOUS, SERVICE
    for _ in range(10):
        usage.charge(SERVICE, "questions")
        usage.charge(ANONYMOUS, "questions")
        usage.charge(None, "questions")


def test_the_count_holds_across_workers(counting):
    p = _Person("u1")
    usage.charge(p, "questions")
    usage._counters = None              # what a second process starts with
    usage.charge(p, "questions")
    usage._counters = None
    usage.charge(p, "questions")
    usage._counters = None
    with pytest.raises(usage.LimitReached):
        usage.charge(p, "questions")


# ---------------------------------------------------------------- through the API

@pytest.fixture
def client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("RATE_LIMITS", "off")
    monkeypatch.setenv("TZ", "UTC")
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import main
    importlib.reload(main)
    from brahmastra import auth
    auth.reset_for_tests()
    usage.reset()
    return TestClient(main.app, raise_server_exceptions=False)


def _member(client, plan_limits: dict, email="m@example.com"):
    from brahmastra.accounts import store
    s = store()
    s.create_user(email, PW, role="member", workspaces=["default"])
    contributor = s.get_plan("contributor")
    s._q("UPDATE plans SET limits = ? WHERE id = 'contributor'",
         (json.dumps({**contributor["limits"], **plan_limits}),))
    r = client.post("/auth/login", json={"email": email, "password": PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_the_api_refuses_the_note_over_the_daily_limit_and_says_why(client):
    h = _member(client, {"notes_day": 2})
    for i in range(2):
        assert client.post("/notes", json={"id": f"n{i}", "title": "t", "content": "a b c"},
                           headers=h).status_code == 201
    r = client.post("/notes", json={"id": "n9", "title": "t", "content": "a b c"}, headers=h)
    assert r.status_code == 429
    body = r.json()
    assert body["code"] == "plan_limit" and body["meter"] == "notes" and body["period"] == "day"
    assert "They reset at midnight" in body["detail"]
    assert int(r.headers["Retry-After"]) > 0


def test_a_failed_question_is_handed_back(client, monkeypatch):
    h = _member(client, {"questions_day": 1})
    import brahmastra.rag as rag

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(rag, "answer_question", boom)
    assert client.post("/ask", json={"question": "who?"}, headers=h).status_code >= 500
    monkeypatch.setattr(rag, "answer_question", lambda *a, **k: {"answer": "Sarah", "source": "graph"})
    assert client.post("/ask", json={"question": "who?"}, headers=h).status_code == 200
    r = client.post("/ask", json={"question": "who?"}, headers=h)
    assert r.status_code == 429 and r.json()["meter"] == "questions"


def test_a_meeting_over_the_monthly_hours_is_refused_before_it_is_stored(client):
    from brahmastra.accounts import store
    s = store()
    u = s.create_user("p@example.com", PW, role="member", workspaces=["default"], plan="power")
    power = s.get_plan("power")
    s._q("UPDATE plans SET limits = ? WHERE id = 'power'",
         (json.dumps({**power["limits"], "meeting_hours_month": 1}),))
    h = {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"email": "p@example.com", "password": PW}).json()["token"]}
    long_meeting = "Ana: " + " ".join(["word"] * 150 * 70)            # about 70 minutes
    r = client.post("/ingest/transcripts", json={"title": "long", "content": long_meeting}, headers=h)
    assert r.status_code == 429 and r.json()["meter"] == "meeting_minutes"
    listed = client.get("/ingest/transcripts", headers=h).json()
    assert (listed.get("transcripts", listed) if isinstance(listed, dict) else listed) == []
    assert u["plan"] == "power"


def test_the_usage_report_shows_each_limit_with_what_is_used(client):
    h = _member(client, {"notes_day": 5})
    client.post("/notes", json={"id": "n1", "title": "t", "content": "one two three four"}, headers=h)
    rows = client.get("/auth/usage", headers=h).json()["usage"]
    notes_day = next(r for r in rows if r["meter"] == "notes" and r["period"] == "day")
    words = next(r for r in rows if r["meter"] == "words")
    assert notes_day["used"] == 1 and notes_day["limit"] == 5
    assert words["used"] == 4 and words["limit"] == 200_000


# ---------------------------------------------------------------- Postgres

from tests._pg import reachable_dsn  # noqa: E402

PG_DSN = reachable_dsn()


@pytest.mark.skipif(PG_DSN is None, reason="BRAHMASTRA_TEST_POSTGRES_DSN not set or not reachable")
def test_the_counter_is_atomic_on_postgres_too(monkeypatch):
    import brahmastra.sidecar as sidecar

    monkeypatch.setattr(sidecar, "note_backend", lambda: "postgres")
    monkeypatch.setenv("POSTGRES_DSN", PG_DSN)
    monkeypatch.setattr(usage, "_limits_for", lambda identity: {"questions_month": 100, "questions_day": 4})
    usage.reset()
    try:
        p, allowed = _Person("pg-usage-user"), []

        def ask():
            try:
                usage.charge(p, "questions")
                allowed.append(1)
            except usage.LimitReached:
                pass

        threads = [threading.Thread(target=ask) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(allowed) == 4
    finally:
        usage.reset()
