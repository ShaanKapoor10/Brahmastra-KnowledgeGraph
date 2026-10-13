"""
API authentication and authorisation: who is calling, and may they do this.

The API exposes the entire graph, can spend LLM tokens, and can write into a
Notion workspace. Exposing it unauthenticated is not a degraded mode, it is a
different product.

Deliberately FAILS CLOSED. This codebase has already been bitten twice by
security that fails open -- workspace isolation leaked silently when a filter
was forgotten, and CORS was a hardcoded "*" -- so the default when nothing is
configured is to refuse, not to allow.

WHO (first match wins):

  Authorization: Bearer bss_...   a dashboard session      (accounts.py)
  Authorization: Bearer bra_...   a personal API token     (accounts.py)
  Authorization: Bearer <key>     BRAHMASTRA_API_KEY, the legacy shared key:
                                  a SERVICE identity with admin rights, kept so
                                  the MCP server, scheduler and scripts work
                                  unchanged. Prefer personal tokens.
  X-API-Key: <any of the above>   for clients that cannot set Authorization

  Auth is ENFORCED once a user exists or BRAHMASTRA_API_KEY is set. With
  neither, every route answers 503 and says how to fix it -- unless
  BRAHMASTRA_ALLOW_ANONYMOUS=1, which opens everything for local development.
  Anonymous mode is ignored once a user exists: creating an account must never
  leave an open door beside it.

WHAT (`required_role`): viewers read, search and ask; members also write,
ingest and run the pipeline; admins also manage users, keys and workspaces.

WHERE: a non-admin may open only the workspaces they are a member of. The
workspace is bound per request before this runs (main.bind_workspace), so the
check here covers every route at once rather than relying on each router to
remember -- a guard at one caller is not a guard.
"""

from __future__ import annotations

import hmac
import os
import time

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

# Probes must answer before a client could hold a token, and an orchestrator's
# health checker has no way to carry one. Login and the status check must be
# reachable by someone who is not logged in yet.
_OPEN_PATHS = frozenset({"/health", "/health/ready", "/auth/login", "/auth/status", "/auth/setup"})

# Routes that act on accounts rather than on a workspace's data.
_WORKSPACE_FREE_PREFIXES = ("/auth/", "/workspaces")

_ADMIN_PREFIXES = ("/auth/users", "/auth/audit", "/diagnostics/keys")
_SELF_SERVICE_PREFIXES = ("/auth/me", "/auth/logout", "/auth/password", "/auth/tokens")
# POSTs a viewer may make: they read and spend, but change nothing stored.
_VIEWER_POSTS = frozenset({"/ask"})


def _configured_key() -> str:
    return os.environ.get("BRAHMASTRA_API_KEY", "").strip()


def _anonymous_allowed() -> bool:
    return os.environ.get("BRAHMASTRA_ALLOW_ANONYMOUS", "").strip().lower() in {"1", "true", "yes"}


def _presented_key(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # MCP clients and simple scripts often find a plain header easier to set
    # than an Authorization header, and both are equally secret over TLS.
    return request.headers.get("x-api-key", "").strip()


# Whether any user exists. Once True it stays True for the process: accounts
# are not expected to drop to zero, and asking the database on every request
# would put a query in front of every call. While False it is rechecked every
# few seconds, so creating the first admin takes effect without a restart.
_users_exist = False
_users_checked_at = 0.0


def users_exist() -> bool:
    global _users_exist, _users_checked_at
    if _users_exist:
        return True
    if time.time() - _users_checked_at > 5:
        from brahmastra.accounts import store

        _users_exist = store().user_count() > 0
        _users_checked_at = time.time()
    return _users_exist


def reset_for_tests() -> None:
    global _users_exist, _users_checked_at
    _users_exist, _users_checked_at = False, 0.0


def auth_status() -> str:
    """For /health/ready, so a deployment can be seen to be protected."""
    try:
        if _configured_key() or users_exist():
            return "enforced"
    except Exception:                                    # noqa: BLE001
        return "enforced"     # cannot tell, and the middleware refuses in that case
    return "anonymous" if _anonymous_allowed() else "unconfigured"


def required_role(method: str, path: str) -> str | None:
    """The least role a request needs; None for any authenticated caller."""
    if path.startswith(_ADMIN_PREFIXES):
        return "admin"
    if path == "/mcp" or path.startswith("/mcp/"):
        # Remote MCP tools run in the MCP server's own task group, so the caller's
        # identity and workspace membership are not proven to reach them, and one
        # tool searches every workspace. Until that is scoped per user, only callers
        # who may see every workspace get in.
        return "admin"
    if path.startswith(_SELF_SERVICE_PREFIXES):
        return None
    if path == "/workspaces" and method == "POST" or (
            path.startswith("/workspaces/") and method in ("DELETE", "PATCH", "PUT")):
        return "admin"
    if method in ("GET", "HEAD") or (method == "POST" and path in _VIEWER_POSTS):
        return "viewer"
    return "member"


def _deny(code: int, detail: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
    return JSONResponse(status_code=code, content={"detail": detail}, headers=headers)


def _resolve(request: Request):
    """(identity, error response). Runs in a thread: it may touch the database."""
    from brahmastra.accounts import ANONYMOUS, SERVICE, store

    presented = _presented_key(request)
    key = _configured_key()
    if presented:
        # compare_digest: a plain == leaks the key one character at a time to
        # an attacker who can measure response timing.
        if key and hmac.compare_digest(presented, key):
            return SERVICE, None
        identity = store().identify(presented)
        if identity:
            return identity, None
        return None, _deny(401, "Invalid, expired or revoked credentials.")
    if key or users_exist():
        return None, _deny(401, "Sign in, or send a token as 'Authorization: Bearer <token>'.")
    if _anonymous_allowed():
        return ANONYMOUS, None
    return None, _deny(503, (
        "API authentication is not configured. Create an admin with "
        "`python -m brahmastra.accounts create-admin --email you@example.com`, set "
        "BRAHMASTRA_API_KEY to require a shared bearer token, or set "
        "BRAHMASTRA_ALLOW_ANONYMOUS=1 to run without authentication (local development only)."))


async def require_api_key(request: Request, call_next):
    """ASGI middleware enforcing the rules in the module docstring."""
    path = request.url.path
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    try:
        identity, error = await run_in_threadpool(_resolve, request)
    except Exception:                                    # noqa: BLE001
        # The account store is unreachable: refusing is the only safe answer.
        return _deny(503, "The account store is unavailable, so the request cannot be authorised.")
    if error is not None:
        return error

    need = required_role(request.method, path)
    if need and not identity.at_least(need):
        return _deny(403, f"This needs the {need} role; you are a {identity.role}.")

    if identity.user_id:
        from brahmastra import ratelimit

        wait = ratelimit.check(identity.user_id, request.method, path)
        if wait is not None:
            return JSONResponse(status_code=429, headers={"Retry-After": str(wait)},
                                content={"detail": f"Too many requests; try again in {wait} s."})

    if not path.startswith(_WORKSPACE_FREE_PREFIXES):
        from brahmastra.workspace import current_workspace

        ws = current_workspace()
        if not identity.can_open(ws):
            return _deny(403, f"You are not a member of the workspace '{ws}'.")

    from brahmastra.accounts import bind_identity, unbind_identity

    request.state.identity = identity
    token = bind_identity(identity)
    try:
        return await call_next(request)
    finally:
        unbind_identity(token)
