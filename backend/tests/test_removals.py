"""
Removing notes and meetings (brahmastra/removals.py).

The rules under test: admins remove anything; a member removes only what is
recorded as theirs, and nothing nobody is recorded against; viewers remove
nothing. Every removal is audited. A note pulled from Notion stays out of later
syncs. Removing is not a plan feature, so a smaller plan never strands what
someone added.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

PW = "correct horse battery"

from brahmastra import removals as _removals  # noqa: E402

REAL_REFRESH = _removals.refresh_graph


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "removals.db"))
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


@pytest.fixture(autouse=True)
def refreshes(monkeypatch):
    """The graph rebuild a removal starts, recorded instead of run: the
    graph-only run itself is tested on its own below."""
    from brahmastra import removals
    calls: list[str] = []
    monkeypatch.setattr(removals, "refresh_graph", lambda ws: calls.append(ws))
    return calls


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
    people = {
        "boss": s.create_user("boss@example.com", PW, role="admin"),
        "mia": s.create_user("mia@example.com", PW, role="member", workspaces=["default"], plan="power"),
        "ola": s.create_user("ola@example.com", PW, role="member", workspaces=["default"], plan="power"),
        "vic": s.create_user("vic@example.com", PW, role="viewer", workspaces=["default"]),
    }
    return {k: (u, _login(client, f"{k}@example.com")) for k, u in people.items()}


def _add_note(client, h, note_id="n-mia", content="Mia owns the export API."):
    r = client.post("/notes", json={"id": note_id, "title": note_id, "content": content}, headers=h)
    assert r.status_code == 201, r.text
    return note_id


def _actions(action):
    return [e for e in _store().audit_entries() if e["action"] == action]


# ---------------------------------------------------------------- notes

def test_a_member_removes_their_own_note_and_it_is_audited(client, team):
    mia, h = team["mia"]
    _add_note(client, h)
    assert client.get("/notes/n-mia", headers=h).json()["added_by"] == mia["id"]

    assert client.delete("/notes/n-mia", headers=h).status_code == 204
    assert client.get("/notes/n-mia", headers=h).status_code == 404
    entry = _actions("note.remove")[0]
    assert entry["actor"] == "mia@example.com" and entry["target"] == "n-mia"
    assert "workspace default" in entry["detail"]


def test_a_member_cannot_remove_someone_elses_note(client, team):
    _add_note(client, team["mia"][1])
    r = client.delete("/notes/n-mia", headers=team["ola"][1])
    assert r.status_code == 403 and "only notes you added" in r.json()["detail"]
    assert client.get("/notes/n-mia", headers=team["mia"][1]).status_code == 200
    assert _actions("note.remove") == []


def test_a_note_nobody_is_recorded_against_is_an_admins_to_remove(client, team):
    from brahmastra import db
    db.upsert_note(id="n-old", title="old", content="Written before authors were recorded.")
    assert client.delete("/notes/n-old", headers=team["mia"][1]).status_code == 403
    assert client.delete("/notes/n-old", headers=team["boss"][1]).status_code == 204
    assert db.get_note("n-old") is None


def test_a_viewer_removes_nothing(client, team):
    _add_note(client, team["mia"][1])
    assert client.delete("/notes/n-mia", headers=team["vic"][1]).status_code == 403


def test_removing_an_unknown_note_is_a_404(client, team):
    assert client.delete("/notes/nope", headers=team["boss"][1]).status_code == 404


def test_removing_is_not_a_plan_feature(client, team):
    mia, h = team["mia"]
    _add_note(client, h)
    _store().set_user_plan(mia["id"], "reader", actor_id=team["boss"][0]["id"], actor="boss@example.com")
    assert client.post("/notes", json={"id": "n2", "title": "t", "content": "c"}, headers=h).status_code == 403
    assert client.delete("/notes/n-mia", headers=h).status_code == 204


def test_a_removed_notion_note_is_not_pulled_back_in(client, team):
    from brahmastra import db, removals, sync
    db.upsert_note(id="page-1", title="Roadmap", content="Roadmap\n\nQ3 ships the export API.",
                   source="notion")
    assert client.delete("/notes/page-1", headers=team["boss"][1]).status_code == 204
    assert removals.removed_ids("notion") == {"page-1"}

    counters = {"synced": 0, "unchanged": 0}
    page = {"id": "page-1", "last_edited_time": "2026-10-20T10:00:00Z"}
    # No client: a removed page is skipped before anything is fetched from Notion.
    sync._process_page(None, page, {}, counters, [], frozenset(), removals.removed_ids("notion"))
    assert counters == {"synced": 0, "unchanged": 1}
    assert db.get_note("page-1") is None
    assert "from notion" in _actions("note.remove")[0]["detail"]


def test_a_note_from_elsewhere_is_not_marked_as_removed_from_a_source(client, team):
    from brahmastra import removals
    _add_note(client, team["mia"][1])
    client.delete("/notes/n-mia", headers=team["mia"][1])
    assert removals.removed_ids("notion") == set()


# ---------------------------------------------------------------- meetings

def _meeting(submitted_by=None, title="Q3 sync"):
    from brahmastra.ingest.store import Transcript, get_ingest_store
    return get_ingest_store().create_transcript(Transcript(
        id="", title=title, content="Mia: We ship on Friday.", submitted_by=submitted_by))


def test_a_member_removes_their_own_meeting_and_it_is_audited(client, team):
    mia, h = team["mia"]
    tid = _meeting(mia["id"])
    listed = client.get("/ingest/transcripts", headers=h).json()
    assert any(t["id"] == tid and t["submitted_by"] == mia["id"] for t in listed)

    r = client.delete(f"/ingest/transcripts/{tid}", headers=h)
    assert r.status_code == 200 and r.json()["deleted"] == tid
    assert client.get(f"/ingest/transcripts/{tid}", headers=h).status_code == 404
    entry = _actions("meeting.remove")[0]
    assert entry["actor"] == "mia@example.com" and "notes kept" in entry["detail"]


def test_a_member_cannot_remove_someone_elses_meeting(client, team):
    tid = _meeting(team["mia"][0]["id"])
    r = client.delete(f"/ingest/transcripts/{tid}", headers=team["ola"][1])
    assert r.status_code == 403 and "only meetings you uploaded" in r.json()["detail"]
    assert client.get(f"/ingest/transcripts/{tid}", headers=team["mia"][1]).status_code == 200


def test_a_meeting_nobody_submitted_is_an_admins_to_remove(client, team):
    tid = _meeting(None)
    assert client.delete(f"/ingest/transcripts/{tid}", headers=team["mia"][1]).status_code == 403
    r = client.delete(f"/ingest/transcripts/{tid}?purge_notes=true", headers=team["boss"][1])
    assert r.status_code == 200
    assert "notes removed" in _actions("meeting.remove")[0]["detail"]


def test_a_viewer_cannot_remove_a_meeting(client, team):
    tid = _meeting(team["vic"][0]["id"])
    assert client.delete(f"/ingest/transcripts/{tid}", headers=team["vic"][1]).status_code == 403


def test_an_uploaded_meeting_records_who_submitted_it(client, team):
    mia, h = team["mia"]
    r = client.post("/ingest/transcripts", json={"title": "Standup", "content": "Mia: Done."}, headers=h)
    assert r.status_code == 200, r.text
    from brahmastra.ingest.store import get_ingest_store
    assert get_ingest_store().get_transcript(r.json()["transcript_id"])["submitted_by"] == mia["id"]


# ---------------------------------------------------------------- the graph follows

def test_a_removal_rebuilds_the_graph_of_its_own_workspace(client, team, refreshes):
    from brahmastra import pipeline
    _add_note(client, team["mia"][1])
    tid = _meeting(team["mia"][0]["id"])
    client.delete("/notes/n-mia", headers=team["mia"][1])
    client.delete(f"/ingest/transcripts/{tid}", headers=team["mia"][1])
    assert refreshes == ["default", "default"]
    assert "removed" in (pipeline.dirty_since() or {}).get("reason", "")


def test_a_refused_removal_rebuilds_nothing(client, team, refreshes):
    _add_note(client, team["mia"][1])
    client.delete("/notes/n-mia", headers=team["ola"][1])
    assert refreshes == []


def test_the_graph_only_run_drops_a_removed_notes_entities_and_reaches_nothing_outside(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "graph-only.db"))
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "0")
    monkeypatch.setenv("NOTION_TOKEN", "secret_not_used")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db-not-used")
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from brahmastra import extraction, notion_writeback, sync

    def forbidden(*a, **k):
        raise AssertionError("a graph-only rebuild must not sync, extract or write back")

    monkeypatch.setattr(sync, "run_sync", forbidden)
    monkeypatch.setattr(extraction, "run_extraction", forbidden)
    monkeypatch.setattr(notion_writeback, "push_insights", forbidden)

    def fact(note, s, o):
        return {"subject_text": s, "subject_type": "person", "relation": "employed_by",
                "object_text": o, "object_type": "organisation", "confidence": 0.9,
                "source_quote": f"{s} works at {o}.", "source_note_id": note}

    db_mod.upsert_note("keep", "keep", "Mei works at Acme.", mark_pending=False)
    db_mod.upsert_note("gone", "gone", "Raj works at Initech.", mark_pending=False)
    db_mod.insert_triples([fact("keep", "Mei", "Acme"), fact("gone", "Raj", "Initech")])

    def names():
        graph = (db_mod.get_cached_graph() or {}).get("graph") or {}
        return {n.get("id") for n in graph.get("nodes", [])}

    first = REAL_REFRESH("default")
    assert first["mode"] == "graph" and "graph" not in first["failed_stages"]
    assert {"Raj", "Initech", "Mei", "Acme"} <= names()

    db_mod.delete_note("gone")
    REAL_REFRESH("default")
    assert "Raj" not in names() and "Initech" not in names()
    assert {"Mei", "Acme"} <= names()


# ---------------------------------------------------------------- the rule itself

def test_may_remove():
    from brahmastra.accounts import ANONYMOUS, SERVICE, Identity
    from brahmastra.removals import may_remove
    mia = Identity(kind="session", role="member", user_id="u1")
    vic = Identity(kind="session", role="viewer", user_id="u2")
    assert may_remove(mia, "u1") and not may_remove(mia, "u9") and not may_remove(mia, None)
    assert not may_remove(vic, "u2")
    assert may_remove(SERVICE, None) and may_remove(ANONYMOUS, "u9")
    assert not may_remove(None, "u1")


# ---------------------------------------------------------------- Postgres

from tests._pg import reachable_dsn  # noqa: E402

PG_DSN = reachable_dsn()


@pytest.mark.skipif(PG_DSN is None, reason="BRAHMASTRA_TEST_POSTGRES_DSN not set or not reachable")
def test_the_new_table_and_column_work_on_postgres(monkeypatch):
    import uuid

    import brahmastra.ingest.store as ingest_store
    import brahmastra.sidecar as sidecar
    from brahmastra import removals

    monkeypatch.setattr(sidecar, "note_backend", lambda: "postgres")
    monkeypatch.setattr(ingest_store, "_backend", lambda: "postgres")
    monkeypatch.setenv("POSTGRES_DSN", PG_DSN)
    ws = f"pg-removals-{uuid.uuid4().hex[:8]}"

    store = removals._Removed(ws)
    store.add("notion", "page-1", "Roadmap", "u1")
    store.add("notion", "page-1", "Roadmap", "u1")          # removing twice is not an error
    assert removals.removed_ids("notion", ws) == {"page-1"}

    s = ingest_store.IngestStore(workspace=ws)
    tid = s.create_transcript(ingest_store.Transcript(id="", title="t", content="A: hi.", submitted_by="u1"))
    try:
        assert s.get_transcript(tid)["submitted_by"] == "u1"
        assert s.list_transcripts()[0]["submitted_by"] == "u1"
    finally:
        s.delete_transcript(tid)
        with store._cursor() as cur:
            cur.execute("DELETE FROM removed_sources WHERE workspace_id = %s", (ws,))
