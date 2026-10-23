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

PLAN: after the role, the person's plan must include the route's feature
(plans.route_feature). A plan narrows what a role allows; it never widens it.

WHERE: a non-admin may open only the workspaces they are a member of. The
workspace is bound per request before this runs (main.bind_workspace), so the
check here covers every route at once rather than relying on each router to
remember -- a guard at one caller is not a guard.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

# Probes must answer before a client could hold a token, and an orchestrator's
# health checker has no way to carry one. Login and the status check must be
# reachable by someone who is not logged in yet.
# /metrics has no token because the scraper sits on the internal network; the
# edge blocks it and the endpoint refuses proxied requests (main.metrics_endpoint).
_OPEN_PATHS = frozenset({"/health", "/health/ready", "/health/uptime", "/livez", "/readyz", "/metrics", "/auth/login",
                         "/auth/status", "/auth/setup"})

# Routes that act on accounts rather than on a workspace's data.
_WORKSPACE_FREE_PREFIXES = ("/auth/", "/workspaces")

# "/auth/usage/" (with the slash) is the admin Usage tab; "/auth/usage" alone
# is a person's own usage and stays self-service.
# Likewise "/auth/plans/<id>" (editing a plan) is admin; "/auth/plans" (reading
# them) is for everyone. "/auth/requests" is the admins' queue; a person's own
# requests live under "/auth/me/".
_ADMIN_PREFIXES = ("/auth/users", "/auth/audit", "/diagnostics/keys", "/auth/usage/", "/auth/plans/",
                   "/auth/requests")
_SELF_SERVICE_PREFIXES = ("/auth/me", "/auth/logout", "/auth/password", "/auth/tokens")
# POSTs a viewer may make: they read and spend, but change nothing stored.
_VIEWER_POSTS = frozenset({"/ask", "/hooks"})


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
        # Any signed-in caller may connect. Each TOOL then re-identifies the
        # caller from the HTTP request and checks its own role and the caller's
        # workspace membership (mcp_server._scoped), because tools run in the MCP
        # session manager's task group, where this middleware's bindings are not
        # guaranteed to reach.
        return "viewer"
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


def resolve_identity(request) -> tuple:
    """(identity, None) or (None, (status, detail)). May touch the database.

    Shared by the middleware and by the remote MCP tools (mcp_server._scoped),
    so there is exactly one answer to "who is this" for both.
    """
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
        return None, (401, "Invalid, expired or revoked credentials.")
    if key or users_exist():
        return None, (401, "Sign in, or send a token as 'Authorization: Bearer <token>'.")
    if _anonymous_allowed():
        return ANONYMOUS, None
    return None, (503, _UNCONFIGURED)


def _resolve(request: Request):
    """(identity, error response). Runs in a thread: it may touch the database."""
    identity, problem = resolve_identity(request)
    return identity, (None if problem is None else _deny(*problem))


_UNCONFIGURED = (
        "API authentication is not configured. Create an admin with "
        "`python -m brahmastra.accounts create-admin --email you@example.com`, set "
        "BRAHMASTRA_API_KEY to require a shared bearer token, or set "
        "BRAHMASTRA_ALLOW_ANONYMOUS=1 to run without authentication (local development only).")


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

    # The plan, AFTER the role: a plan can only narrow what a role allows.
    from brahmastra.plans import refusal, route_feature

    feature = route_feature(request.method, path)
    if feature and not identity.has(feature):
        return JSONResponse(status_code=403, content={"detail": refusal(feature), "code": "plan_feature",
                                                      "feature": feature})

    # A question is counted against the plan BEFORE it is answered, so two
    # parallel requests cannot both take the last one, and handed back below
    # if the answer fails. Notes and meetings are counted where they enter,
    # because their size is in the request body.
    charged = False
    asking = request.method == "POST" and path == "/ask"
    if identity.user_id and asking:
        from brahmastra import usage

        try:
            # A spent token budget refuses the question before it is counted.
            problem = await run_in_threadpool(usage.budget_problem, identity.user_id)
            if problem:
                return JSONResponse(status_code=429, content={"detail": problem, "code": "token_budget"})
            await run_in_threadpool(usage.charge, identity, "questions")
            charged = True
        except usage.LimitReached as e:
            return JSONResponse(status_code=429, content=e.body(),
                                headers={"Retry-After": str(max(1, int(e.reset_at - time.time())))})
        except Exception:                               # noqa: BLE001
            # The counter is unreachable: a meter must never take the API down.
            pass

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
    from brahmastra import usage

    purpose = usage.charging(purpose="ask") if asking else contextlib.nullcontext()
    try:
        with purpose:
            response = await call_next(request)
    except Exception:
        # A route that raises reaches here as an exception, not as a 500.
        if charged:
            await _refund_question(identity)
        raise
    finally:
        unbind_identity(token)
    if charged and response.status_code >= 400:
        await _refund_question(identity)
    return response


async def _refund_question(identity) -> None:
    """A failed question was not answered, so it does not count."""
    from brahmastra import usage

    try:
        await run_in_threadpool(usage.refund, identity, "questions")
    except Exception:                                   # noqa: BLE001
        pass
