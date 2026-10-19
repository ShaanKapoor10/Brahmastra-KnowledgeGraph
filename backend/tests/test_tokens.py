"""
Tokens, budgets and the admin Usage tab (phase C of plans).

What must hold: every billed token is booked to whoever the work was for (a
question to its asker, a note to its author even when a background run reads
it, a meeting to its submitter, shared graph work to the workspace); a spent
budget stops the next AI call before it starts, never falls back to another
provider, and loses no note; and the Usage tab adds up to exactly what was
booked, with no text in it.
"""
from __future__ import annotations

import importlib

import pytest

from brahmastra import usage

PW = "correct horse battery"


@pytest.fixture
def booked(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setenv("TZ", "UTC")
    for key in ("TOKEN_BUDGET_DAY", "TOKEN_BUDGET_MONTH"):
        monkeypatch.delenv(key, raising=False)
    usage.reset()
    yield
    usage.reset()


def _ledger():
    return usage._store().rows("SELECT * FROM usage_ledger")


def _as(user_id):
    """Bind a signed-in person, as the auth middleware does."""
    from brahmastra.accounts import Identity, bind_identity
    return bind_identity(Identity(kind="session", role="member", user_id=user_id))


def _fake_provider(monkeypatch, tokens=(100, 20, 5)):
    """A provider that bills `tokens` per call, through the real recording path."""
    from brahmastra import llm

    def dispatch(name, system, user, **kw):
        llm._record_tokens(name, *tokens)
        return '{"triples": []}'

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    return llm


# ---------------------------------------------------------------- who pays

def test_a_call_is_booked_to_the_person_making_the_request(booked, monkeypatch):
    from brahmastra.accounts import unbind_identity
    llm = _fake_provider(monkeypatch)
    token = _as("u-ana")
    try:
        with usage.charging(purpose="ask"):
            llm.chat("s", "u")
    finally:
        unbind_identity(token)
    [row] = _ledger()
    assert (row["who"], row["purpose"], row["provider"]) == ("u-ana", "ask", "groq")
    assert (row["input"], row["output"], row["thoughts"], row["calls"]) == (100, 20, 5, 1)
    month = usage.periods()["month"][0]
    assert usage._store().totals("u-ana", [month])[("tokens", month)] == 125
    assert usage._store().totals(usage.COMPANY, [month])[("tokens", month)] == 125


def test_shared_graph_work_is_booked_to_the_workspace_not_whoever_pressed_run(booked, monkeypatch):
    from brahmastra.accounts import unbind_identity
    llm = _fake_provider(monkeypatch)
    token = _as("u-ana")
    try:
        with usage.charging(workspace="office", purpose="graph"):
            llm.chat("s", "u")
    finally:
        unbind_identity(token)
    assert {r["who"] for r in _ledger()} == {"ws:office"}


def test_reading_a_note_is_booked_to_its_author_even_in_a_background_run(booked, monkeypatch):
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from brahmastra import extraction
    from brahmastra.accounts import unbind_identity
    _fake_provider(monkeypatch)
    monkeypatch.setattr(extraction, "_extract_with_llm",
                        lambda title, content: (__import__("brahmastra.llm", fromlist=["chat"]).chat("s", content),
                                                {"triples": []})[1])

    token = _as("u-ben")                         # Ben adds the note...
    try:
        db_mod.upsert_note("n1", "t", "Ben wrote this.")
    finally:
        unbind_identity(token)
    assert usage.author_of("n1") == "u-ben"

    extraction.extract_note(db_mod.get_note("n1"))   # ...a background run reads it, with no identity
    assert [(r["who"], r["purpose"]) for r in _ledger()] == [("u-ben", "note")]


def test_a_meeting_is_booked_to_whoever_submitted_it(booked, monkeypatch):
    from brahmastra.ingest import routes
    llm = _fake_provider(monkeypatch)
    monkeypatch.setattr(routes, "process_transcript", lambda tid, workspace: llm.chat("s", "u"))
    routes._launch("t1", "default", "u-cara")
    assert [(r["who"], r["purpose"]) for r in _ledger()] == [("u-cara", "meeting")]


def test_a_cache_hit_is_counted_as_a_call_that_cost_nothing(booked):
    usage.record_cache_hit()
    [row] = _ledger()
    assert row["provider"] == "cache" and row["calls"] == 1 and row["input"] == 0


# ---------------------------------------------------------------- budgets

def test_a_spent_personal_budget_stops_the_next_call_and_never_falls_back(booked, monkeypatch):
    from brahmastra.accounts import unbind_identity
    llm = _fake_provider(monkeypatch)
    monkeypatch.setattr(usage, "_person_limits", lambda uid: {"tokens_day": 200, "tokens_month": 10_000})
    fell_back = []
    monkeypatch.setattr(llm, "_next_usable_provider", lambda name: fell_back.append(name) or "ollama")
    token = _as("u-ana")
    try:
        llm.chat("s", "u")
        llm.chat("s", "u")                      # 250 booked: over today's 200
        with pytest.raises(llm.BudgetExhausted, match="Your AI usage for today is used up"):
            llm.chat("s", "u")
    finally:
        unbind_identity(token)
    assert fell_back == []
    # Classified as a spent quota, so extraction stops the run and the note stays to retry.
    from brahmastra.extraction import _is_quota_error
    assert _is_quota_error("429 token budget spent: Your AI usage for today is used up.")


def test_shared_work_meets_only_the_company_budget(booked, monkeypatch):
    monkeypatch.setattr(usage, "_person_limits", lambda uid: {"tokens_day": 1})
    assert usage.budget_problem("ws:office") is None
    usage.set_company_budget(day=100, month=None)
    usage._store().add(usage.COMPANY, "tokens", usage.periods()["day"][0], 100)
    assert "The company's AI budget for today" in usage.budget_problem("ws:office")


def test_the_server_setting_caps_what_an_admin_can_set(booked, monkeypatch):
    monkeypatch.setenv("TOKEN_BUDGET_MONTH", "500000")
    assert usage.set_company_budget(day=None, month=9_000_000) == {"day": None, "month": 500000}
    assert usage.set_company_budget(day=1000, month=100) == {"day": 1000, "month": 100}


def test_a_note_over_budget_stays_and_is_retried_not_lost(booked, monkeypatch):
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from brahmastra import extraction
    _fake_provider(monkeypatch)
    usage.set_company_budget(day=0, month=None)                 # nothing left today
    db_mod.upsert_note("n2", "t", "Kept for tomorrow.")
    from brahmastra import llm as llm_mod
    monkeypatch.setattr(extraction, "_extract_with_llm", lambda t, c: llm_mod.chat("s", c))
    result = extraction.extract_note(db_mod.get_note("n2"))
    assert "token budget" in result["error"]
    note = db_mod.get_note("n2")
    assert note["content"] == "Kept for tomorrow." and note["extraction_status"] == "error"


# ---------------------------------------------------------------- the API and the Usage tab

@pytest.fixture
def client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("RATE_LIMITS", "off")
    monkeypatch.setenv("TZ", "UTC")
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN",
                "TOKEN_BUDGET_DAY", "TOKEN_BUDGET_MONTH"):
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


def _login(client, email):
    return {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"email": email, "password": PW}).json()["token"]}


def test_a_question_over_budget_is_refused_before_it_is_counted(client, monkeypatch):
    from brahmastra.accounts import store
    u = store().create_user("m@example.com", PW, role="member", workspaces=["default"])
    h = _login(client, "m@example.com")
    monkeypatch.setattr(usage, "_person_limits", lambda uid: {"tokens_day": 10})
    usage._store().add(u["id"], "tokens", usage.periods()["day"][0], 10)
    r = client.post("/ask", json={"question": "who?"}, headers=h)
    assert r.status_code == 429 and r.json()["code"] == "token_budget"
    assert "resets at midnight" in r.json()["detail"]
    month = usage.periods()["month"][0]
    assert usage._store().totals(u["id"], [month]).get(("questions", month), 0) == 0


def test_the_usage_tab_adds_up_to_what_was_booked_and_only_admins_see_it(client):
    from brahmastra.accounts import store
    s = store()
    s.create_user("boss@example.com", PW, role="admin")
    ana = s.create_user("ana@example.com", PW, role="member", workspaces=["default"])
    usage.set_company_budget(day=None, month=1_000_000)
    with usage.charging(user_id=ana["id"], purpose="ask"):
        usage.record_tokens("groq", "openai/gpt-oss-120b", 1000, 200, 50)
    with usage.charging(workspace="default", purpose="summary"):
        usage.record_tokens("groq", "openai/gpt-oss-120b", 400, 100, 0)

    member, boss = _login(client, "ana@example.com"), _login(client, "boss@example.com")
    assert client.get("/auth/usage/overview", headers=member).status_code == 403
    assert client.get("/auth/usage", headers=member).status_code == 200

    data = client.get("/auth/usage/overview", headers=boss).json()
    assert data["company"]["month"]["tokens"] == 1750
    assert data["company"]["budget"]["month"] == 1_000_000
    ana_row = next(p for p in data["people"] if p["email"] == "ana@example.com")
    assert (ana_row["tokens"], ana_row["tokens_today"]) == (1250, 1250)
    assert ana_row["cost"] == pytest.approx((1000 * 0.15 + 250 * 0.60) / 1e6, abs=1e-6)
    assert [w["key"] for w in data["workspaces"]] == ["ws:default"]
    assert {g["key"] for g in data["by_purpose"]} == {"ask", "summary"}
    assert data["series"][-1]["input"] == 1400

    csv = client.get("/auth/usage/export.csv", headers=boss)
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("email,name,plan")
    assert "ana@example.com" in csv.text

    r = client.put("/auth/usage/budget", json={"tokens_day": 5000, "tokens_month": 90000}, headers=boss)
    assert r.json() == {"day": 5000, "month": 90000}
    assert any(e["action"] == "budget.set" for e in s.audit_entries())


def test_the_usage_tab_carries_no_text():
    """Counts only: no column of the ledger can hold note text, a question or an answer."""
    schema = usage._Counters.SCHEMA
    ledger = schema[schema.index("usage_ledger"):schema.index("note_authors")]
    assert not any(word in ledger for word in ("content", "question", "answer", "prompt", "title"))


# ---------------------------------------------------------------- calls that bypass llm.chat

def test_groq_extraction_books_to_the_author_and_respects_the_budget(booked, monkeypatch):
    """Extraction talks to Groq directly (its 413 handling is bespoke), so it
    must meter itself: found by the phase C hands-on check, where a note read
    by the pipeline booked nothing at all."""
    pytest.importorskip("groq")
    import types as pytypes

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from brahmastra import extraction, groq_pool, llm
    from brahmastra.accounts import unbind_identity

    class Pool:
        def call(self, fn):
            usage_ = pytypes.SimpleNamespace(prompt_tokens=700, completion_tokens=90,
                                             completion_tokens_details=pytypes.SimpleNamespace(reasoning_tokens=40))
            msg = pytypes.SimpleNamespace(content='{"triples": []}')
            client = pytypes.SimpleNamespace(chat=pytypes.SimpleNamespace(completions=pytypes.SimpleNamespace(
                create=lambda **kw: pytypes.SimpleNamespace(choices=[pytypes.SimpleNamespace(message=msg)],
                                                            usage=usage_))))
            return fn(client)

    monkeypatch.setattr(groq_pool, "pool", lambda: Pool())
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setenv("LLM_MEMO", "0")

    token = _as("u-dev")
    try:
        db_mod.upsert_note("n3", "t", "Dev wrote this one.")
    finally:
        unbind_identity(token)
    extraction.extract_note(db_mod.get_note("n3"))
    [row] = _ledger()
    assert (row["who"], row["purpose"], row["provider"]) == ("u-dev", "note", "groq")
    assert (row["input"], row["output"], row["thoughts"]) == (700, 50, 40)

    usage.set_company_budget(day=1, month=None)                 # now spent
    db_mod.upsert_note("n4", "t", "Another note.")
    result = extraction.extract_note(db_mod.get_note("n4"))
    assert "token budget" in (result["error"] or "")
    assert len(_ledger()) == 1                                  # nothing new was billed


def test_no_code_calls_a_provider_without_metering_it():
    """Every direct provider call outside llm.py must sit in a module that also
    checks the budget and books the tokens -- or it is invisible to plans."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "brahmastra"
    calls = ("completions.create(", "messages.create(", "generate_content(")
    for path in root.rglob("*.py"):
        if path.name == "llm.py":
            continue
        text = path.read_text(encoding="utf-8")
        if any(c in text for c in calls):
            assert "_check_budget" in text and "_record_" in text, \
                f"{path.relative_to(root)} calls a provider directly without budget check and token booking"
