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
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def at_least(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]

    def can_open(self, workspace_id: str) -> bool:
        return self.workspaces is None or workspace_id in self.workspaces

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "role": self.role, "user_id": self.user_id, "email": self.email,
                "name": self.name,
                "workspaces": None if self.workspaces is None else sorted(self.workspaces)}


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
                    workspaces: list[str] | None = None, actor: str | None = None) -> dict[str, Any]:
        email = _norm_email(email)
        if role not in ROLES:
            raise AccountError(f"Role must be one of {', '.join(ROLES)}.")
        check_password_strength(password)
        if self._q("SELECT id FROM users WHERE email = ?", (email,), one=True):
            raise AccountError("A user with that email already exists.")
        uid = uuid.uuid4().hex
        self._q("INSERT INTO users (id, email, name, password_hash, role, disabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)", (uid, email, name, hash_password(password), role, time.time()))
        for ws in workspaces or []:
            self.add_member(uid, ws)
        self.audit(actor, "user.create", email, f"role={role}")
        return self.get_user(uid)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        u = self._q("SELECT id, email, name, role, disabled, created_at, last_login_at FROM users WHERE id = ?",
                    (user_id,), one=True)
        if u:
            u["disabled"] = bool(u["disabled"])
            u["workspaces"] = self.member_workspaces(user_id)
        return u

    def find_user(self, email: str) -> dict[str, Any] | None:
        try:
            email = _norm_email(email)
        except AccountError:
            return None
        u = self._q("SELECT id FROM users WHERE email = ?", (email,), one=True)
        return self.get_user(u["id"]) if u else None

    def list_users(self) -> list[dict[str, Any]]:
        ids = self._q("SELECT id FROM users ORDER BY created_at", rows=True)
        return [self.get_user(r["id"]) for r in ids]

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
        return self.get_user(user_id)

    def delete_user(self, user_id: str, actor: str | None = None) -> None:
        user = self.get_user(user_id)
        if not user:
            raise AccountError("No such user.")
        if user["role"] == "admin" and not user["disabled"] and self._active_admins() <= 1:
            raise AccountError("This is the last active admin; make someone else an admin first.")
        self.revoke_all(user_id)
        self._q("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM workspace_members WHERE user_id = ?", (user_id,))
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
        user = self.get_user(row["id"])
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
        return Identity(kind=kind, role=u["role"], user_id=u["id"], email=u["email"], name=u["name"],
                        workspaces=ws, token_id=token_id)

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
    a = ap.parse_args(argv)
    s = store()
    try:
        if a.cmd == "list":
            for u in s.list_users():
                flags = " (disabled)" if u["disabled"] else ""
                print(f"{u['email']:40} {u['role']:7}{flags}  workspaces: {', '.join(u['workspaces']) or '-'}")
            return 0
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat it: "):
            print("The passwords did not match.")
            return 1
        if a.cmd == "create-admin":
            u = s.create_user(a.email, pw, role="admin", name=a.name, actor="cli")
            print(f"Created admin {u['email']}.")
        else:
            u = s.find_user(a.email)
            if not u:
                print("No such user.")
                return 1
            s.set_password(u["id"], pw, actor="cli")
            print(f"Password reset for {u['email']}; their sessions were ended.")
        return 0
    except AccountError as e:
        print(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
