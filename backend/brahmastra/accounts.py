"""
User accounts: who is calling, what they may do, and which workspaces they see.

Brahmastra began as one person's memory behind one shared API key. A team
install needs more than that key can say:

  * WHO made a change -- a shared key makes every write anonymous;
  * WHAT they may do -- a reader should not run the pipeline or delete notes;
  * WHICH graphs they see -- the payroll workspace is not everyone's.

And the key itself had a worse problem: the dashboard's proxy attached it to
every browser request, so anyone who could open the dashboard held the whole
API. Accounts are what let the dashboard carry a person's own session instead.

WHAT LIVES HERE (tables beside the notes, via SidecarStore):

    users              email, scrypt password hash, role, disabled
    sessions           dashboard logins: token HASH, expiry
    api_tokens         personal tokens for MCP clients and scripts: HASH only
    workspace_members  which workspaces a non-admin may open
    audit_log          logins, account changes, token changes
    login_failures     per email+IP, for lockout

This is SOURCE data -- a lost user table cannot be recomputed -- so it lives in
the system of record with the notes, never in Neo4j.

ROLES. One global role per user; workspace membership decides which graphs a
non-admin sees.

    viewer   read, search and ask
    member   viewer + write notes, ingest sessions, run the pipeline
    admin    member + every workspace, users, keys, workspace management

SECRETS ARE NEVER STORED. Passwords are scrypt-hashed (stdlib, so no new
dependency, with parameters stored in the hash so they can be raised later);
session and API tokens are 256-bit random values stored as SHA-256 hashes. A
leaked database therefore yields no usable credential. A token is shown once,
when it is created.

    python -m brahmastra.accounts create-admin --email you@example.com
    python -m brahmastra.accounts list
    python -m brahmastra.accounts reset-password --email you@example.com
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from brahmastra.sidecar import SidecarStore

ROLES = ("viewer", "member", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_PREFIX = "bss_"
TOKEN_PREFIX = "bra_"
SESSION_TTL = 14 * 24 * 3600            # absolute lifetime of a dashboard login
MIN_PASSWORD = 10
LOCKOUT_FAILURES = 5                    # failures per email+IP ...
LOCKOUT_WINDOW = 15 * 60                # ... within this many seconds locks it

# scrypt: N=2^15, r=8, p=1 costs ~32 MB and ~50-100 ms per check -- slow enough
# to make offline guessing expensive, fast enough for a login.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


class AccountError(ValueError):
    """A request about accounts that cannot be honoured; the message is safe to show."""


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()          # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                                r=int(r), p=int(p), maxmem=64 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against when the email is unknown, so a wrong email and a wrong
# password take the same time and a login form cannot be used to list users.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")
    if password.lower() in {"password123", "changeme123", "brahmastra1"} or len(set(password)) < 4:
        raise AccountError("That password is too easy to guess.")


def _token_hash(token: str) -> str:
    # SHA-256 is right HERE, and wrong for passwords. A token is 32 random bytes
    # from `secrets`, so there is nothing to guess: a slow hash only protects
    # low-entropy secrets, and would cost scrypt on every authenticated request.
    # Passwords use scrypt (hash_password). CodeQL alert #5 was dismissed for this.
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254 or " " in email:
        raise AccountError("Enter a valid email address.")
    return email


# ---------------------------------------------------------------- identity

@dataclass(frozen=True)
class Identity:
    """Who a request is from. `workspaces` None means every workspace."""

    kind: str                         # session | token | service | anonymous
    role: str
    user_id: str | None = None
    email: str | None = None
    name: str | None = None
    workspaces: frozenset[str] | None = None
    token_id: str | None = None
    # The features this person's plan includes (plans.FEATURES); None means
    # every feature -- the service key and anonymous development mode.
    features: frozenset[str] | None = None
    plan: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def at_least(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]

    def has(self, feature: str) -> bool:
        return self.features is None or feature in self.features

    def can_open(self, workspace_id: str) -> bool:
        return self.workspaces is None or workspace_id in self.workspaces

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "role": self.role, "user_id": self.user_id, "email": self.email,
                "name": self.name,
                "workspaces": None if self.workspaces is None else sorted(self.workspaces),
                "plan": self.plan,
                "features": None if self.features is None else sorted(self.features)}


SERVICE = Identity(kind="service", role="admin", name="service key")
ANONYMOUS = Identity(kind="anonymous", role="admin", name="anonymous (development)")

_current: ContextVar[Identity | None] = ContextVar("brahmastra_identity", default=None)


def current_identity() -> Identity | None:
    """The identity of the request being served, or None outside a request."""
    return _current.get()


def bind_identity(identity: Identity | None):
    return _current.set(identity)


def unbind_identity(token) -> None:
    _current.reset(token)


# ---------------------------------------------------------------- the store

class AccountStore(SidecarStore):
    """
    Accounts are global, not per workspace, so the workspace SidecarStore binds
    is ignored: no query here filters by it.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL, last_login_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL, last_seen_at DOUBLE PRECISION, user_agent TEXT);
    CREATE TABLE IF NOT EXISTS api_tokens (
        id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
        name TEXT NOT NULL, prefix TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION, revoked_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS workspace_members (
        user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, PRIMARY KEY (user_id, workspace_id));
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, actor TEXT, action TEXT NOT NULL,
        target TEXT, detail TEXT, ip TEXT);
    CREATE TABLE IF NOT EXISTS login_failures (
        key TEXT PRIMARY KEY, count INTEGER NOT NULL, first_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, features TEXT NOT NULL,
        limits TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS user_plans (
        user_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, assigned_by TEXT,
        assigned_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION, return_plan_id TEXT);
    CREATE TABLE IF NOT EXISTS plan_requests (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, plan_id TEXT,
        limit_key TEXT, amount BIGINT, reason TEXT NOT NULL, status TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, decided_by TEXT, decided_at DOUBLE PRECISION,
        decision_note TEXT, expires_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS plan_grants (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, limit_key TEXT NOT NULL, amount BIGINT NOT NULL,
        period TEXT NOT NULL, granted_by TEXT, request_id TEXT, created_at DOUBLE PRECISION NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_plan_requests_status ON plan_requests (status);
    CREATE INDEX IF NOT EXISTS idx_plan_grants_user ON plan_grants (user_id, period);
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
    CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens (user_id);
    CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at)
    """

    def __init__(self) -> None:
        super().__init__(workspace="default")

    def _q(self, sql: str, params: tuple = (), *, one: bool = False, rows: bool = False):
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(sql), params)
            if one:
                r = cur.fetchone()
                return self._dict(r) if r else None
            if rows:
                return [self._dict(r) for r in cur.fetchall()]
            return None

    # -- users
    def user_count(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users", one=True)["n"])

    def create_user(self, email: str, password: str, role: str = "member", name: str | None = None,
                    workspaces: list[str] | None = None, actor: str | None = None,
                    plan: str | None = None) -> dict[str, Any]:
        email = _norm_email(email)
        if role not in ROLES:
            raise AccountError(f"Role must be one of {', '.join(ROLES)}.")
        check_password_strength(password)
        if self._q("SELECT id FROM users WHERE email = ?", (email,), one=True):
            raise AccountError("A user with that email already exists.")
        if plan is not None and self.get_plan(plan) is None:
            raise AccountError("No such plan.")
        # Before the insert: seeding maps EXISTING users by role, and this
        # person is new, so they must get the new-user plan instead.
        self.ensure_plans()
        uid = uuid.uuid4().hex
        self._q("INSERT INTO users (id, email, name, password_hash, role, disabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)", (uid, email, name, hash_password(password), role, time.time()))
        for ws in workspaces or []:
            self.add_member(uid, ws)
        from brahmastra.plans import plan_for_new_user

        self._q("INSERT INTO user_plans (user_id, plan_id, assigned_by, assigned_at) VALUES (?, ?, ?, ?)",
                (uid, plan or plan_for_new_user(role), actor, time.time()))
        self.audit(actor, "user.create", email, f"role={role}, plan={plan or plan_for_new_user(role)}")
        return self._require_user(uid)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        u = self._q("SELECT id, email, name, role, disabled, created_at, last_login_at FROM users WHERE id = ?",
                    (user_id,), one=True)
        if u:
            u["disabled"] = bool(u["disabled"])
            u["workspaces"] = self.member_workspaces(user_id)
            u["plan"] = self.plan_id_for(user_id, u["role"])
            row = self._q("SELECT expires_at FROM user_plans WHERE user_id = ?", (user_id,), one=True)
            u["plan_expires_at"] = row["expires_at"] if row else None
        return u

    def _require_user(self, user_id: str) -> dict[str, Any]:
        """The user, which the caller has just created, changed or authenticated."""
        user = self.get_user(user_id)
        if user is None:
            raise AccountError("No such user.")
        return user

    def find_user(self, email: str) -> dict[str, Any] | None:
        try:
            email = _norm_email(email)
        except AccountError:
            return None
        u = self._q("SELECT id FROM users WHERE email = ?", (email,), one=True)
        return self.get_user(u["id"]) if u else None

    def list_users(self) -> list[dict[str, Any]]:
        ids = self._q("SELECT id FROM users ORDER BY created_at", rows=True)
        # A user deleted between the two reads is simply not listed.
        return [u for r in ids if (u := self.get_user(r["id"])) is not None]

    def update_user(self, user_id: str, *, role: str | None = None, disabled: bool | None = None,
                    name: str | None = None, workspaces: list[str] | None = None,
                    actor: str | None = None) -> dict[str, Any]:
        user = self.get_user(user_id)
        if not user:
            raise AccountError("No such user.")
        if role is not None and role not in ROLES:
            raise AccountError(f"Role must be one of {', '.join(ROLES)}.")
        demoting = (role is not None and role != "admin") or disabled
        if user["role"] == "admin" and demoting and self._active_admins() <= 1:
            # A team install with no admin left can only be recovered from the server.
            raise AccountError("This is the last active admin; make someone else an admin first.")
        if role is not None:
            self._q("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if disabled is not None:
            self._q("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
            if disabled:
                self.revoke_all(user_id)
        if name is not None:
            self._q("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        if workspaces is not None:
            self._q("DELETE FROM workspace_members WHERE user_id = ?", (user_id,))
            for ws in workspaces:
                self.add_member(user_id, ws)
        self.audit(actor, "user.update", user["email"],
                   ", ".join(f"{k}={v}" for k, v in (("role", role), ("disabled", disabled),
                                                      ("workspaces", workspaces)) if v is not None))
        return self._require_user(user_id)

    def delete_user(self, user_id: str, actor: str | None = None) -> None:
        user = self.get_user(user_id)
        if not user:
            raise AccountError("No such user.")
        if user["role"] == "admin" and not user["disabled"] and self._active_admins() <= 1:
            raise AccountError("This is the last active admin; make someone else an admin first.")
        self.revoke_all(user_id)
        self._q("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM workspace_members WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM user_plans WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM plan_requests WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM plan_grants WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM users WHERE id = ?", (user_id,))
        self.audit(actor, "user.delete", user["email"])

    def set_password(self, user_id: str, password: str, actor: str | None = None) -> None:
        check_password_strength(password)
        self._q("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
        # A new password ends every existing login: if the old one leaked, so did its sessions.
        self.revoke_all(user_id)
        user = self.get_user(user_id)
        self.audit(actor, "user.password", user["email"] if user else user_id)

    def _active_admins(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0",
                           one=True)["n"])

    # -- workspace membership
    def add_member(self, user_id: str, workspace_id: str) -> None:
        from brahmastra.workspace import validate_id

        ws = validate_id(workspace_id)
        if not self._q("SELECT 1 AS x FROM workspace_members WHERE user_id = ? AND workspace_id = ?",
                       (user_id, ws), one=True):
            self._q("INSERT INTO workspace_members (user_id, workspace_id) VALUES (?, ?)", (user_id, ws))

    def member_workspaces(self, user_id: str) -> list[str]:
        return [r["workspace_id"] for r in self._q(
            "SELECT workspace_id FROM workspace_members WHERE user_id = ? ORDER BY workspace_id",
            (user_id,), rows=True)]

    # -- login and sessions
    def _locked(self, key: str) -> bool:
        row = self._q("SELECT count, first_at FROM login_failures WHERE key = ?", (key,), one=True)
        if not row:
            return False
        if time.time() - row["first_at"] > LOCKOUT_WINDOW:
            self._q("DELETE FROM login_failures WHERE key = ?", (key,))
            return False
        return row["count"] >= LOCKOUT_FAILURES

    def _fail(self, key: str) -> None:
        row = self._q("SELECT count, first_at FROM login_failures WHERE key = ?", (key,), one=True)
        if not row or time.time() - row["first_at"] > LOCKOUT_WINDOW:
            self._q("DELETE FROM login_failures WHERE key = ?", (key,))
            self._q("INSERT INTO login_failures (key, count, first_at) VALUES (?, 1, ?)", (key, time.time()))
        else:
            self._q("UPDATE login_failures SET count = count + 1 WHERE key = ?", (key,))

    def check_credentials(self, email: str, password: str, ip: str = "") -> str:
        """The user id for valid credentials. One generic error for every failure,
        and failures count towards the per email+IP lockout."""
        key = f"{(email or '').strip().lower()}|{ip}"
        if self._locked(key):
            self.audit(None, "login.locked", (email or "").strip().lower(), ip=ip)
            raise AccountError("Too many failed attempts. Try again in 15 minutes.")
        row = None
        try:
            row = self._q("SELECT id, password_hash, disabled FROM users WHERE email = ?",
                          (_norm_email(email),), one=True)
        except AccountError:
            pass
        ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
        if not row or not ok or row["disabled"]:
            self._fail(key)
            self.audit(None, "login.failed", (email or "").strip().lower(), ip=ip)
            raise AccountError("Email or password is incorrect.")
        self._q("DELETE FROM login_failures WHERE key = ?", (key,))
        return row["id"]

    def login(self, email: str, password: str, ip: str = "", user_agent: str = "") -> dict[str, Any]:
        """A new session for valid credentials."""
        row = {"id": self.check_credentials(email, password, ip)}
        token = SESSION_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        self._q("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)", (_token_hash(token), row["id"], now, now + SESSION_TTL, now,
                                              (user_agent or "")[:200]))
        self._q("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
        user = self._require_user(row["id"])
        self.audit(user["email"], "login", user["email"], ip=ip)
        return {"token": token, "expires_at": now + SESSION_TTL, "user": user}

    def logout(self, token: str) -> None:
        self._q("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def revoke_all(self, user_id: str) -> None:
        self._q("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # -- API tokens
    def create_token(self, user_id: str, name: str, actor: str | None = None) -> dict[str, Any]:
        name = (name or "").strip()[:80]
        if not name:
            raise AccountError("Give the token a name, such as the machine or client it is for.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tid = uuid.uuid4().hex[:12]
        self._q("INSERT INTO api_tokens (id, token_hash, user_id, name, prefix, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (tid, _token_hash(token), user_id, name, token[:8], time.time()))
        self.audit(actor, "token.create", name)
        # The only time the whole token exists outside the client.
        return {"id": tid, "name": name, "token": token, "prefix": token[:8]}

    def list_tokens(self, user_id: str) -> list[dict[str, Any]]:
        return self._q("SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_tokens "
                       "WHERE user_id = ? ORDER BY created_at DESC", (user_id,), rows=True)

    def revoke_token(self, user_id: str, token_id: str, actor: str | None = None) -> None:
        row = self._q("SELECT name FROM api_tokens WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                      (token_id, user_id), one=True)
        if not row:
            raise AccountError("No such active token.")
        self._q("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (time.time(), token_id))
        self.audit(actor, "token.revoke", row["name"])

    # -- resolving a presented credential
    def identify(self, credential: str) -> Identity | None:
        """The identity behind a session or API token, or None if it is not valid now."""
        if not credential:
            return None
        h, now = _token_hash(credential), time.time()
        if credential.startswith(SESSION_PREFIX):
            s = self._q("SELECT user_id, expires_at FROM sessions WHERE token_hash = ?", (h,), one=True)
            if not s or s["expires_at"] <= now:
                return None
            self._q("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, h))
            return self._identity(s["user_id"], "session")
        if credential.startswith(TOKEN_PREFIX):
            t = self._q("SELECT id, user_id FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                        (h,), one=True)
            if not t:
                return None
            self._q("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, t["id"]))
            return self._identity(t["user_id"], "token", token_id=t["id"])
        return None

    def _identity(self, user_id: str, kind: str, token_id: str | None = None) -> Identity | None:
        u = self._q("SELECT id, email, name, role, disabled FROM users WHERE id = ?", (user_id,), one=True)
        if not u or u["disabled"]:
            return None
        ws = None if u["role"] == "admin" else frozenset(self.member_workspaces(user_id))
        plan = self.plan_for(user_id, u["role"])
        return Identity(kind=kind, role=u["role"], user_id=u["id"], email=u["email"], name=u["name"],
                        workspaces=ws, token_id=token_id, plan=plan["id"],
                        features=frozenset(plan["features"]))

    # -- plans (see plans.py for what they mean)
    def ensure_plans(self) -> None:
        """Create the starting plans once, and give every existing person the plan
        matching their role, so introducing plans takes nobody's access away.

        Runs only while the plans table is empty. Every insert ignores a row that
        is already there, so two workers starting at once cannot double it.
        """
        if self._q("SELECT id FROM plans LIMIT 1", one=True):
            return
        from brahmastra.plans import DEFAULT_PLANS, ROLE_PLAN

        now = time.time()
        for position, plan in enumerate(DEFAULT_PLANS):
            self._q("INSERT INTO plans (id, name, description, features, limits, position, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                    (plan["id"], plan["name"], plan["description"], json.dumps(plan["features"]),
                     json.dumps(plan["limits"]), position, now, now))
        for u in self._q("SELECT id, role FROM users", rows=True):
            self._q("INSERT INTO user_plans (user_id, plan_id, assigned_by, assigned_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO NOTHING",
                    (u["id"], ROLE_PLAN.get(u["role"], "reader"), "rollout", now))
        self.audit("system", "plans.seed", None, ", ".join(p["id"] for p in DEFAULT_PLANS))

    @staticmethod
    def _plan_row(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "description": row["description"],
                "features": json.loads(row["features"]), "limits": json.loads(row["limits"])}

    def list_plans(self) -> list[dict[str, Any]]:
        self.ensure_plans()
        return [self._plan_row(r) for r in self._q("SELECT * FROM plans ORDER BY position, id", rows=True)]

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        self.ensure_plans()
        row = self._q("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
        return self._plan_row(row) if row else None

    def plan_id_for(self, user_id: str, role: str) -> str:
        """The plan a person is on. No row (a user created before plans by some
        path that missed the rollout) falls back to the role's plan."""
        from brahmastra.plans import ROLE_PLAN

        self.ensure_plans()
        row = self._q("SELECT plan_id, expires_at, return_plan_id FROM user_plans WHERE user_id = ?",
                      (user_id,), one=True)
        if row and row["expires_at"] and row["expires_at"] <= time.time():
            # A time-limited plan has ended: back to the plan it replaced. Done
            # on the first look after the end, so no scheduler has to remember.
            back = row["return_plan_id"] or ROLE_PLAN.get(role, "reader")
            self._q("UPDATE user_plans SET plan_id = ?, expires_at = NULL, return_plan_id = NULL, "
                    "assigned_by = ?, assigned_at = ? WHERE user_id = ? AND plan_id = ?",
                    (back, "expiry", time.time(), user_id, row["plan_id"]))
            user = self._q("SELECT email FROM users WHERE id = ?", (user_id,), one=True)
            self.audit("system", "plan.expire", user["email"] if user else user_id, f"{row['plan_id']} -> {back}")
            return back
        return row["plan_id"] if row else ROLE_PLAN.get(role, "reader")

    def plan_for(self, user_id: str, role: str) -> dict[str, Any]:
        plan = self.get_plan(self.plan_id_for(user_id, role))
        if plan is None:
            # The plan was deleted out from under the person: the least plan,
            # never an error that locks them out of reading.
            from brahmastra.plans import DEFAULT_PLANS

            plan = dict(DEFAULT_PLANS[0])
        return plan

    def set_user_plan(self, user_id: str, plan_id: str, *, actor_id: str | None,
                      actor: str | None, expires_at: float | None = None) -> dict[str, Any]:
        """Put a person on a plan. Nobody changes their own plan, so every upgrade
        has a second person behind it -- except a sole admin, who has nobody to
        ask; the audit log records that it was self-assigned."""
        user = self.get_user(user_id)
        if not user:
            raise AccountError("No such user.")
        plan = self.get_plan(plan_id)
        if not plan:
            raise AccountError("No such plan.")
        self_assigned = actor_id is not None and actor_id == user_id
        if self_assigned and self._active_admins() > 1:
            raise AccountError("You cannot change your own plan; ask another admin.")
        if expires_at is not None and expires_at <= time.time():
            raise AccountError("The end date must be in the future.")
        before = user["plan"]
        self._q("DELETE FROM user_plans WHERE user_id = ?", (user_id,))
        self._q("INSERT INTO user_plans (user_id, plan_id, assigned_by, assigned_at, expires_at, return_plan_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, plan_id, actor, time.time(), expires_at, before if expires_at else None))
        until = f" until {time.strftime('%Y-%m-%d', time.gmtime(expires_at))}" if expires_at else ""
        self.audit(actor, "plan.assign", user["email"],
                   f"{before} -> {plan_id}{until}" + (" (self-assigned: sole admin)" if self_assigned else ""))
        return self._require_user(user_id)

    def effective_limits(self, user_id: str, role: str) -> dict[str, Any]:
        """The plan's limits plus any top-up granted for the current day or month.
        Every limit check reads this, so a top-up cannot be missed by one of them."""
        from brahmastra.usage import periods

        limits = dict(self.plan_for(user_id, role)["limits"])
        now = periods()
        rows = self._q("SELECT limit_key, SUM(amount) AS extra FROM plan_grants WHERE user_id = ? "
                       "AND period IN (?, ?) GROUP BY limit_key",
                       (user_id, now["day"][0], now["month"][0]), rows=True)
        for r in rows:
            if limits.get(r["limit_key"]) is not None:
                limits[r["limit_key"]] = int(limits[r["limit_key"]]) + int(r["extra"])
        return limits

    def update_plan(self, plan_id: str, *, actor: str | None, name: str | None = None,
                    description: str | None = None, features: list[str] | None = None,
                    limits: dict[str, Any] | None = None) -> dict[str, Any]:
        """Change a plan for everyone on it. Validated; the audit records what changed."""
        from brahmastra.plans import LIMITS, validate

        plan = self.get_plan(plan_id)
        if not plan:
            raise AccountError("No such plan.")
        new_features = plan["features"] if features is None else list(dict.fromkeys(features))
        new_limits = dict(plan["limits"])
        if limits is not None:
            new_limits.update(limits)
        try:
            validate(new_features, new_limits)
        except ValueError as e:
            raise AccountError(str(e)) from e
        if plan_id == "unlimited" and set(new_features) != set(plan["features"]):
            # The admins' plan: taking a feature away from it could leave nobody
            # able to restore it from the screen.
            raise AccountError("The Unlimited plan's features cannot be changed.")
        changes = []
        if set(new_features) != set(plan["features"]):
            added, removed = set(new_features) - set(plan["features"]), set(plan["features"]) - set(new_features)
            changes.append(" ".join([*(f"+{f}" for f in sorted(added)), *(f"-{f}" for f in sorted(removed))]))
        for key in LIMITS:
            if new_limits.get(key) != plan["limits"].get(key):
                changes.append(f"{key}: {plan['limits'].get(key)} -> {new_limits.get(key)}")
        self._q("UPDATE plans SET name = ?, description = ?, features = ?, limits = ?, updated_at = ? WHERE id = ?",
                ((name or plan["name"]).strip()[:60], description if description is not None else plan["description"],
                 json.dumps(new_features), json.dumps(new_limits), time.time(), plan_id))
        self.audit(actor, "plan.edit", plan_id, "; ".join(changes) or "name or description")
        return self.get_plan(plan_id)  # type: ignore[return-value]

    # -- upgrade requests: people ask, admins decide, everything is recorded
    def _request_row(self, row: dict[str, Any]) -> dict[str, Any]:
        user = self._q("SELECT email, name FROM users WHERE id = ?", (row["user_id"],), one=True) or {}
        return {**row, "email": user.get("email"), "name": user.get("name")}

    def create_request(self, user_id: str, *, kind: str, reason: str, plan_id: str | None = None,
                       limit_key: str | None = None, amount: int | None = None) -> dict[str, Any]:
        from brahmastra.plans import LIMITS

        user = self.get_user(user_id)
        if not user:
            raise AccountError("No such user.")
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise AccountError("Say briefly why you need it.")
        if kind == "plan":
            if not plan_id or not self.get_plan(plan_id):
                raise AccountError("No such plan.")
            if plan_id == user["plan"]:
                raise AccountError("You are already on that plan.")
            limit_key, amount = None, None
        elif kind == "topup":
            if limit_key not in LIMITS:
                raise AccountError("Choose which limit to raise.")
            if not isinstance(amount, int) or amount <= 0:
                raise AccountError("The extra amount must be a whole number above 0.")
            plan_id = None
        else:
            raise AccountError("A request is for a plan or a top-up.")
        if self._q("SELECT id FROM plan_requests WHERE user_id = ? AND kind = ? AND status = 'pending'",
                   (user_id, kind), one=True):
            raise AccountError("You already have a request like this waiting; cancel it to ask differently.")
        rid = uuid.uuid4().hex
        self._q("INSERT INTO plan_requests (id, user_id, kind, plan_id, limit_key, amount, reason, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (rid, user_id, kind, plan_id, limit_key, amount, reason[:500], time.time()))
        what = plan_id if kind == "plan" else f"+{amount} {limit_key}"
        self.audit(user["email"], "plan.request", user["email"], f"{kind}: {what}")
        return self.get_request(rid)  # type: ignore[return-value]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        row = self._q("SELECT * FROM plan_requests WHERE id = ?", (request_id,), one=True)
        return self._request_row(row) if row else None

    def list_requests(self, *, status: str | None = None, user_id: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        sql = ("SELECT * FROM plan_requests" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY created_at DESC LIMIT ?")
        return [self._request_row(r) for r in self._q(sql, (*params, max(1, min(limit, 1000))), rows=True)]

    def cancel_request(self, user_id: str, request_id: str) -> None:
        req = self.get_request(request_id)
        if not req or req["user_id"] != user_id:
            raise AccountError("No such request.")
        if req["status"] != "pending":
            raise AccountError("Only a waiting request can be cancelled.")
        self._q("UPDATE plan_requests SET status = 'cancelled', decided_at = ? WHERE id = ?", (time.time(), request_id))
        self.audit(req["email"], "plan.request.cancel", req["email"], request_id)

    def decide_request(self, request_id: str, *, approve: bool, actor_id: str | None, actor: str | None,
                       note: str | None = None, expires_at: float | None = None) -> dict[str, Any]:
        """Approve or decline. Same rule as assigning a plan: nobody approves their
        own request while another admin exists to do it."""
        req = self.get_request(request_id)
        if not req:
            raise AccountError("No such request.")
        if req["status"] != "pending":
            raise AccountError(f"This request was already {req['status']}.")
        if actor_id == req["user_id"] and self._active_admins() > 1:
            raise AccountError("You cannot decide your own request; ask another admin.")
        note = (note or "").strip()[:500] or None
        if approve:
            if req["kind"] == "plan":
                self.set_user_plan(req["user_id"], req["plan_id"], actor_id=None, actor=actor,
                                   expires_at=expires_at)
            else:
                from brahmastra.usage import periods

                period = periods()["day" if req["limit_key"].endswith("_day") else "month"][0]
                self._q("INSERT INTO plan_grants (id, user_id, limit_key, amount, period, granted_by, request_id, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (uuid.uuid4().hex, req["user_id"], req["limit_key"], req["amount"], period, actor,
                         request_id, time.time()))
        status = "approved" if approve else "declined"
        self._q("UPDATE plan_requests SET status = ?, decided_by = ?, decided_at = ?, decision_note = ?, "
                "expires_at = ? WHERE id = ?", (status, actor, time.time(), note, expires_at, request_id))
        what = req["plan_id"] if req["kind"] == "plan" else f"+{req['amount']} {req['limit_key']}"
        self.audit(actor, f"plan.request.{'approve' if approve else 'decline'}", req["email"],
                   f"{req['kind']}: {what}" + (f" ({note})" if note else ""))
        return self.get_request(request_id)  # type: ignore[return-value]

    # -- audit
    def audit(self, actor: str | None, action: str, target: str | None = None, detail: str = "",
              ip: str = "") -> None:
        self._q("INSERT INTO audit_log (id, at, actor, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, time.time(), actor, action, target, detail[:500], ip))

    def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._q("SELECT at, actor, action, target, detail, ip FROM audit_log ORDER BY at DESC LIMIT ?",
                       (max(1, min(int(limit), 1000)),), rows=True)


_store: AccountStore | None = None


def store() -> AccountStore:
    """One AccountStore per process; it holds no connection between calls."""
    global _store
    if _store is None:
        _store = AccountStore()
    return _store


def reset_store_for_tests() -> None:
    global _store
    _store = None


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(prog="python -m brahmastra.accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-admin", help="create an admin (the first one, or another)")
    c.add_argument("--email", required=True)
    c.add_argument("--name")
    sub.add_parser("list", help="list users")
    r = sub.add_parser("reset-password", help="set a new password and end the user's sessions")
    r.add_argument("--email", required=True)
    for sp in (c, r):
        # For automation (Ansible, CI): read the password from stdin, like
        # `docker login --password-stdin`, so it never appears in argv or history.
        sp.add_argument("--password-stdin", action="store_true")
    a = ap.parse_args(argv)
    s = store()
    try:
        if a.cmd == "list":
            for u in s.list_users():
                flags = " (disabled)" if u["disabled"] else ""
                print(f"{u['email']:40} {u['role']:7}{flags}  workspaces: {', '.join(u['workspaces']) or '-'}")
            return 0
        if a.password_stdin:
            import sys

            pw = sys.stdin.readline().rstrip("\r\n")
        else:
            pw = getpass.getpass("New password: ")
            if pw != getpass.getpass("Repeat it: "):
                print("The passwords did not match.")
                return 1
        if a.cmd == "create-admin":
            created = s.create_user(a.email, pw, role="admin", name=a.name, actor="cli")
            print(f"Created admin {created['email']}.")
        else:
            target = s.find_user(a.email)
            if not target:
                print("No such user.")
                return 1
            s.set_password(target["id"], pw, actor="cli")
            print(f"Password reset for {target['email']}; their sessions were ended.")
        return 0
    except AccountError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
