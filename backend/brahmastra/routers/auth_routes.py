"""
Accounts over HTTP: sign in, personal API tokens, and (for admins) users.

    GET    /auth/status                 open: is auth on, does setup remain
    POST   /auth/setup                  open, ONLY with BRAHMASTRA_SETUP_TOKEN and no users yet
    POST   /auth/login                  open: email + password -> session token
    POST   /auth/logout                 end this session
    GET    /auth/me                     who am I
    POST   /auth/password               change my password (needs the current one)
    GET    /auth/tokens                 my API tokens (never the secret)
    POST   /auth/tokens                 new token: the secret is returned ONCE
    DELETE /auth/tokens/{id}            revoke one
    GET    /auth/users                  admin: list
    POST   /auth/users                  admin: create
    PATCH  /auth/users/{id}             admin: role, disabled, name, workspaces
    DELETE /auth/users/{id}             admin
    POST   /auth/users/{id}/password    admin: set a new password (ends their sessions)
    GET    /auth/audit                  admin: recent account events
    GET    /auth/plans                  every plan, with its features and limits
    GET    /auth/usage                  my use this day and month against my plan's limits
    GET    /auth/usage/overview         admin: tokens by day, person, workspace, purpose, model
    GET    /auth/usage/users/{id}       admin: one person's daily history
    GET    /auth/usage/export.csv       admin: this month's people table as CSV
    GET    /auth/usage/budget           admin: the company's token budgets
    PUT    /auth/usage/budget           admin: set them (capped by TOKEN_BUDGET_DAY/_MONTH)
    PUT    /auth/users/{id}/plan        admin: put someone on a plan (not yourself)

Who may call what is decided in auth.required_role, not here, so these handlers
never contain a permission check that a refactor could drop.

Why /auth/setup needs a token: on a fresh public deployment with no users, an
endpoint that makes "the first caller" an admin hands the install to whoever
finds it first. The first admin is normally created on the server with
`python -m brahmastra.accounts create-admin`; the HTTP route exists only for
hosts without a shell, and only while BRAHMASTRA_SETUP_TOKEN is set.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from brahmastra import accounts
from brahmastra.accounts import AccountError

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=1024)


class SetupRequest(Credentials):
    name: str | None = Field(default=None, max_length=120)
    setup_token: str = Field(..., max_length=512)


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=1024)
    new_password: str = Field(..., max_length=1024)


class NewToken(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class NewUser(Credentials):
    name: str | None = Field(default=None, max_length=120)
    role: str = "member"
    workspaces: list[str] = Field(default_factory=list)
    plan: str | None = Field(default=None, max_length=64)   # None: the role's plan


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    name: str | None = Field(default=None, max_length=120)
    workspaces: list[str] | None = None


class Budget(BaseModel):
    tokens_day: int | None = Field(default=None, ge=0)
    tokens_month: int | None = Field(default=None, ge=0)


class PlanChoice(BaseModel):
    plan_id: str = Field(..., min_length=1, max_length=64)


class NewPassword(BaseModel):
    password: str = Field(..., max_length=1024)


def _ip(request: Request) -> str:
    # Behind a reverse proxy every request comes from the proxy, which would put
    # all users in one lockout bucket. The forwarded address is trusted only when
    # the deployment says a proxy sets it, because a client can send the header too.
    if os.environ.get("TRUST_PROXY_HEADERS", "").strip() == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


def _me(request: Request) -> accounts.Identity:
    return request.state.identity


def _actor(request: Request) -> str:
    ident = getattr(request.state, "identity", None)
    return (ident.email or ident.kind) if ident else "anonymous"


def _user_only(request: Request) -> accounts.Identity:
    me = _me(request)
    if not me.user_id:
        raise HTTPException(400, "This needs a signed-in user, not a service key.")
    return me


def _fail(e: AccountError, code: int = 400):
    raise HTTPException(code, str(e)) from e


@router.get("/status")
def status_() -> dict[str, Any]:
    from brahmastra.auth import _anonymous_allowed, _configured_key, auth_status, users_exist

    has_users = users_exist()
    return {
        "auth": auth_status(),
        "users": has_users,
        "setup_required": not has_users and not _configured_key() and not _anonymous_allowed(),
        "setup_via_http": bool(os.environ.get("BRAHMASTRA_SETUP_TOKEN", "").strip()) and not has_users,
    }


@router.post("/setup", status_code=201)
def setup(body: SetupRequest, request: Request) -> dict[str, Any]:
    expected = os.environ.get("BRAHMASTRA_SETUP_TOKEN", "").strip()
    s = accounts.store()
    if not expected or s.user_count() > 0:
        raise HTTPException(404, "Not available.")
    if not hmac.compare_digest(body.setup_token.strip(), expected):
        s.audit(None, "setup.failed", body.email, ip=_ip(request))
        raise HTTPException(403, "Wrong setup token.")
    try:
        s.create_user(body.email, body.password, role="admin", name=body.name, actor="setup")
        return s.login(body.email, body.password, ip=_ip(request),
                       user_agent=request.headers.get("user-agent", ""))
    except AccountError as e:
        _fail(e)


@router.post("/login")
def login(body: Credentials, request: Request) -> dict[str, Any]:
    try:
        return accounts.store().login(body.email, body.password, ip=_ip(request),
                                      user_agent=request.headers.get("user-agent", ""))
    except AccountError as e:
        code = 429 if "Too many" in str(e) else 401
        _fail(e, code)


@router.post("/logout", status_code=204)
def logout(request: Request) -> None:
    from brahmastra.auth import _presented_key

    token = _presented_key(request)
    if token.startswith(accounts.SESSION_PREFIX):
        accounts.store().logout(token)


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    return _me(request).public()


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request) -> None:
    me_ = _user_only(request)
    s = accounts.store()
    try:
        s.check_credentials(me_.email, body.current_password, ip=_ip(request))
    except AccountError as e:
        _fail(AccountError("The current password is incorrect.") if "incorrect" in str(e) else e, 403)
    try:
        s.set_password(me_.user_id, body.new_password, actor=me_.email)
    except AccountError as e:
        _fail(e)


@router.get("/tokens")
def list_tokens(request: Request) -> dict[str, Any]:
    return {"tokens": accounts.store().list_tokens(_user_only(request).user_id)}


@router.post("/tokens", status_code=201)
def create_token(body: NewToken, request: Request) -> dict[str, Any]:
    me_ = _user_only(request)
    try:
        return accounts.store().create_token(me_.user_id, body.name, actor=me_.email)
    except AccountError as e:
        _fail(e)


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: str, request: Request) -> None:
    me_ = _user_only(request)
    try:
        accounts.store().revoke_token(me_.user_id, token_id, actor=me_.email)
    except AccountError as e:
        _fail(e, 404)


@router.get("/users")
def list_users() -> dict[str, Any]:
    return {"users": accounts.store().list_users(), "roles": list(accounts.ROLES)}


@router.post("/users", status_code=201)
def create_user(body: NewUser, request: Request) -> dict[str, Any]:
    try:
        return accounts.store().create_user(body.email, body.password, role=body.role, name=body.name,
                                            workspaces=body.workspaces, actor=_actor(request),
                                            plan=body.plan)
    except AccountError as e:
        _fail(e)


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserPatch, request: Request) -> dict[str, Any]:
    try:
        return accounts.store().update_user(user_id, role=body.role, disabled=body.disabled, name=body.name,
                                            workspaces=body.workspaces, actor=_actor(request))
    except AccountError as e:
        _fail(e, 404 if "No such" in str(e) else 400)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, request: Request) -> None:
    try:
        accounts.store().delete_user(user_id, actor=_actor(request))
    except AccountError as e:
        _fail(e, 404 if "No such" in str(e) else 400)


@router.post("/users/{user_id}/password", status_code=204)
def reset_password(user_id: str, body: NewPassword, request: Request) -> None:
    s = accounts.store()
    if not s.get_user(user_id):
        raise HTTPException(404, "No such user.")
    try:
        s.set_password(user_id, body.password, actor=_actor(request))
    except AccountError as e:
        _fail(e)


@router.get("/plans")
def list_plans() -> dict[str, Any]:
    from brahmastra.plans import FEATURES, LIMITS

    return {"plans": accounts.store().list_plans(), "features": FEATURES, "limits": LIMITS}


@router.get("/usage")
def my_usage(request: Request) -> dict[str, Any]:
    from brahmastra import usage

    me_ = _me(request)
    if not me_.user_id:
        return {"plan": None, "usage": []}
    return {"plan": me_.plan, "usage": usage.report(me_.user_id, me_.role)}


@router.get("/usage/overview")
def usage_overview(days: int = 30) -> dict[str, Any]:
    from brahmastra import usage

    return usage.overview(days)


@router.get("/usage/users/{user_id}")
def usage_person(user_id: str, days: int = 30) -> dict[str, Any]:
    from brahmastra import usage

    if not accounts.store().get_user(user_id):
        raise HTTPException(404, "No such user.")
    return usage.person(user_id, days)


@router.get("/usage/export.csv")
def usage_export() -> Any:
    import csv
    import io

    from fastapi.responses import Response

    from brahmastra import usage

    data = usage.overview(1)
    out = io.StringIO()
    fields = ["email", "name", "plan", "role", "questions", "notes", "words", "meeting_hours",
              "input", "output", "thoughts", "tokens", "calls", "cost"]
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in data["people"]:
        writer.writerow({**row, "cost": row["cost"] if row["priced"] else ""})
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="brahmastra-usage-{data["month"]}.csv"'})


@router.get("/usage/budget")
def get_budget() -> dict[str, Any]:
    from brahmastra import usage

    return usage.company_budget()


@router.put("/usage/budget")
def put_budget(body: Budget, request: Request) -> dict[str, Any]:
    from brahmastra import usage

    out = usage.set_company_budget(body.tokens_day, body.tokens_month)
    accounts.store().audit(_actor(request), "budget.set", "company",
                           f"day={out['day']}, month={out['month']}")
    return out


@router.put("/users/{user_id}/plan")
def set_plan(user_id: str, body: PlanChoice, request: Request) -> dict[str, Any]:
    me_ = _me(request)
    try:
        return accounts.store().set_user_plan(user_id, body.plan_id, actor_id=me_.user_id,
                                              actor=_actor(request))
    except AccountError as e:
        _fail(e, 404 if "No such" in str(e) else 403)


@router.get("/audit")
def audit(limit: int = 200) -> dict[str, Any]:
    return {"entries": accounts.store().audit_entries(limit)}
