"""
Remote MCP is scoped to the caller: each tool call is checked against the
caller's own role and workspace membership, over a real MCP session.

Remote MCP used to be admin-only, because tools run in the MCP session
manager's task group and the API middleware's identity binding was not proven
to reach them. These tests prove the replacement end to end -- a real
streamable-HTTP handshake, then tool calls carrying each user's own token and
workspace header -- rather than by calling the decorator with a stub.

One test function on purpose: an MCP session manager can be run only once per
process, so every scenario shares one app lifespan.
"""
from __future__ import annotations

import importlib
import json

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402

PW = "correct horse battery"
ACCEPT = "application/json, text/event-stream"


def _rpc_result(response) -> dict:
    """The JSON-RPC message from a streamable-HTTP reply (SSE or plain JSON)."""
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    assert data, response.text
    return json.loads(data[-1])


class McpSession:
    def __init__(self, client: TestClient, token: str, workspace: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT,
                        "X-Brahmastra-Workspace": workspace}
        r = client.post("/mcp/", headers=self.headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        _rpc_result(r)
        self.headers["mcp-session-id"] = r.headers["mcp-session-id"]
        client.post("/mcp/", headers=self.headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._id = 1

    def call(self, tool: str, **arguments) -> str:
        self._id += 1
        r = self.client.post("/mcp/", headers=self.headers, json={
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}})
        message = _rpc_result(r)
        assert "result" in message, message
        return "".join(part.get("text", "") for part in message["result"]["content"])


@pytest.fixture
def mcp_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "mcp.db"))
    monkeypatch.setenv("MCP_HTTP", "1")
    monkeypatch.setenv("RATE_LIMITS", "off")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "testserver")      # the TestClient's Host header
    for key in ("BRAHMASTRA_API_KEY", "BRAHMASTRA_ALLOW_ANONYMOUS", "BRAHMASTRA_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from brahmastra.workspace import reset_request_workspace, set_request_workspace
    for ws, text in (("alpha", "Apollo launch moved to June"), ("beta", "Apollo budget is confidential")):
        db_mod.create_workspace(ws, ws)
        token = set_request_workspace(ws)
        try:
            db_mod.upsert_note(f"{ws}-1", f"{ws} note", text)
        finally:
            reset_request_workspace(token)

    from brahmastra import accounts, auth, mcp_server
    importlib.reload(mcp_server)          # a fresh FastMCP: its session manager runs once
    import main
    importlib.reload(main)
    auth.reset_for_tests()
    return main.app, accounts.store(), db_mod


def test_remote_mcp_tools_are_scoped_to_the_caller(mcp_app):
    app, store, db_mod = mcp_app
    admin = store.create_user("admin@example.com", PW, role="admin")
    member = store.create_user("mia@example.com", PW, role="member", workspaces=["alpha"])
    viewer = store.create_user("vic@example.com", PW, role="viewer", workspaces=["alpha"])
    # A member whose PLAN is narrower than her role: the role allows writing.
    reader = store.create_user("rita@example.com", PW, role="member", workspaces=["alpha"], plan="reader")
    tok = {u["email"]: store.create_token(u["id"], "test")["token"] for u in (admin, member, viewer, reader)}

    with TestClient(app, raise_server_exceptions=False) as client:
        # A member reads their own workspace through MCP...
        mia = McpSession(client, tok["mia@example.com"], "alpha")
        assert "Apollo launch moved to June" in mia.call("brahmastra_search_notes", query="Apollo")

        # ...never sees another one, not even through the cross-workspace search...
        across = json.loads(mia.call("brahmastra_search_all_workspaces", query="Apollo"))
        assert {h["workspace"] for h in across["results"]} == {"alpha"}
        listed = json.loads(mia.call("brahmastra_list_workspaces"))
        assert {w["id"] for w in listed["workspaces"]} == {"alpha"}

        # ...and what they write lands in the workspace they named, not `default`.
        assert "error" not in mia.call("brahmastra_add_note", title="From MCP", content="Written remotely")
        from brahmastra.workspace import reset_request_workspace, set_request_workspace
        t = set_request_workspace("alpha")
        try:
            assert any(n["title"] == "From MCP" for n in db_mod.get_notes())
        finally:
            reset_request_workspace(t)

        # Notes added by an assistant count against the plan like any other:
        # with one note a day, the second is refused with the reason.
        import json as _json
        contributor = store.get_plan("contributor")
        store._q("UPDATE plans SET limits = ? WHERE id = 'contributor'",
                 (_json.dumps({**contributor["limits"], "notes_day": 1}),))
        assert "You've used your 1 notes today" in mia.call("brahmastra_add_note", title="Again",
                                                             content="Second note")

        # Opening a workspace they are not a member of is refused before any tool runs.
        r = client.post("/mcp/", headers={"Authorization": f"Bearer {tok['mia@example.com']}",
                                          "Accept": ACCEPT, "X-Brahmastra-Workspace": "beta"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 403

        # Roles hold per tool: a viewer may read but not write or run the pipeline.
        vic = McpSession(client, tok["vic@example.com"], "alpha")
        assert "Apollo" in vic.call("brahmastra_search_notes", query="Apollo")
        assert "needs the member role" in vic.call("brahmastra_add_note", title="x", content="y")
        assert "needs the member role" in vic.call("brahmastra_run_pipeline")
        assert "needs the admin role" in mia.call("brahmastra_create_workspace", id="gamma", name="Gamma")

        # Plans hold per tool as well: a Reader's assistant searches, but the
        # plan refuses the note her role would allow.
        rita = McpSession(client, tok["rita@example.com"], "alpha")
        assert "Apollo" in rita.call("brahmastra_search_notes", query="Apollo")
        assert "Add notes is not in your plan" in rita.call("brahmastra_add_note", title="x", content="y")

        # An admin sees every workspace.
        boss = McpSession(client, tok["admin@example.com"], "alpha")
        across = json.loads(boss.call("brahmastra_search_all_workspaces", query="Apollo"))
        assert {"alpha", "beta"} <= {h["workspace"] for h in across["results"]}

        # A revoked token stops working mid-session, at the next call.
        store.revoke_token(member["id"], store.list_tokens(member["id"])[0]["id"])
        r = client.post("/mcp/", headers=mia.headers, json={
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "brahmastra_search_notes", "arguments": {"query": "Apollo"}}})
        assert r.status_code == 401


def test_remote_mcp_accepts_this_installs_own_host_and_nothing_else():
    """Behind Caddy the Host header is the install's domain. The SDK's default
    (localhost only) refused every remote request there with 421."""
    from brahmastra.mcp_server import allowed_hosts_and_origins

    hosts, origins = allowed_hosts_and_origins(
        {"PUBLIC_URL": "https://kb.example.com", "DOMAIN": "kb.example.com"})
    # Exactly these, and nothing else: the install's own name and localhost,
    # each on any port.
    names = {"kb.example.com", "localhost", "127.0.0.1", "[::1]"}
    assert set(hosts) == names | {f"{n}:*" for n in names}
    assert origins == ["https://kb.example.com"]

    hosts, _ = allowed_hosts_and_origins({"MCP_ALLOWED_HOSTS": "mcp.internal, other.host"})
    assert {"mcp.internal", "other.host"} <= set(hosts)
    _, origins = allowed_hosts_and_origins({"CORS_ORIGINS": "*"})
    assert origins == []                   # "*" is never an allowed origin


def test_the_documented_url_without_a_slash_is_served_not_redirected(mcp_app):
    """Clients are told https://DOMAIN/v1/mcp. A 307 to /mcp/ is built from what
    the backend saw behind Caddy -- plain http, /v1 stripped -- so following it
    lands on a URL that does not exist (found 2026-10-22). Every other test
    here posts to /mcp/, which is why nothing caught it."""
    app, store, _ = mcp_app
    admin = store.create_user("admin@example.com", PW, role="admin")
    token = store.create_token(admin["id"], "test")["token"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/mcp", headers=headers, json=body, follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "brahmastra" in r.text
